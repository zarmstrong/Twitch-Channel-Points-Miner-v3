"""Drop claim threads (a PubSub drop-claim event, a drop that reached 100%)
stop with the miner: none starts once it stops, one waiting to claim ends
without claiming, and shutdown waits for a claim Twitch is still answering
before it stops the History journal (#418)."""

import importlib
from threading import Event, Lock
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.test_twitch_drop_claim import claim_recording_twitch
from TwitchChannelPointsMiner.classes.Twitch import Twitch
from TwitchChannelPointsMiner.TwitchChannelPointsMiner import TwitchChannelPointsMiner

twitch_module = importlib.import_module("TwitchChannelPointsMiner.classes.Twitch")
MINER_MODULE = "TwitchChannelPointsMiner.TwitchChannelPointsMiner"
CLAIM_ALL_DROPS = Twitch.claim_all_drops_from_inventory
# Twitch has __slots__: the inventory sweeps each one ran, by instance.
CLAIMS = {}


def claimed(twitch):
    return CLAIMS.setdefault(id(twitch), [])


DROP = SimpleNamespace(name="Reward", minutes_required=10, drop_instance_id="i-1")
CAMPAIGN = SimpleNamespace(id="campaign-1", name="Campaign")


@pytest.fixture
def twitch(monkeypatch):
    twitch = object.__new__(Twitch)
    twitch.running = True
    twitch.prompt_claim_lock = Lock()
    twitch.prompt_claim_pass_lock = Lock()
    twitch.prompt_claim_last = {}
    twitch.drop_claim_stop_event = Event()
    monkeypatch.setattr(
        Twitch,
        "claim_all_drops_from_inventory",
        lambda self: claimed(self).append("claim"),
    )
    # No waiting for the inventory to catch up.
    monkeypatch.setattr(twitch_module.random, "uniform", lambda low, high: 0)
    CLAIMS.clear()
    yield twitch
    # Never leave a claim thread behind.
    twitch.running = False
    assert twitch.join_drop_claim_workers(timeout=5) == []


def finish(twitch):
    # Wait for the claim threads to end on their own, without stopping them.
    for thread in list(twitch.drop_claim_threads):
        thread.join(5)
        assert not thread.is_alive()


def start_event_claim(twitch):
    twitch.claim_drops_from_event({"drop_instance_id": "i-1"})


def start_prompt_claim(twitch):
    twitch._Twitch__claim_completed_drop_promptly(DROP, CAMPAIGN)


@pytest.mark.parametrize("start", [start_event_claim, start_prompt_claim])
def test_both_claim_threads_claim_and_are_waited_for(twitch, start):
    start(twitch)
    finish(twitch)
    assert claimed(twitch) == ["claim"]
    assert twitch.drop_claim_threads == set()
    assert twitch.join_drop_claim_workers(timeout=5) == []


@pytest.mark.parametrize("start", [start_event_claim, start_prompt_claim])
def test_no_claim_thread_starts_once_the_miner_stops(twitch, monkeypatch, start):
    started = []
    monkeypatch.setattr(
        twitch_module, "Thread", lambda **kwargs: started.append(kwargs)
    )
    twitch.running = False
    start(twitch)
    assert started == []
    # A prompt claim that never ran is not debounced.
    assert twitch.prompt_claim_last == {}


def test_no_claim_thread_starts_after_the_workers_were_stopped(twitch, monkeypatch):
    started = []
    monkeypatch.setattr(
        twitch_module, "Thread", lambda **kwargs: started.append(kwargs)
    )
    twitch.stop_drop_claim_workers()
    start_event_claim(twitch)
    start_prompt_claim(twitch)
    assert started == []


def test_a_claim_thread_waiting_for_the_inventory_ends_on_stop(twitch, monkeypatch):
    # A long wait for the inventory to show the drop: the stop ends it.
    monkeypatch.setattr(twitch_module.random, "uniform", lambda low, high: 600)
    start_event_claim(twitch)
    (thread,) = twitch.drop_claim_threads
    twitch.running = False
    assert twitch.join_drop_claim_workers(timeout=5) == []
    assert not thread.is_alive()
    assert claimed(twitch) == []


def test_a_claim_thread_waiting_for_a_claim_pass_ends_on_stop(twitch, monkeypatch):
    monkeypatch.setattr(twitch_module, "DROP_CLAIM_LOCK_POLL_SECONDS", 0.01)
    waited = Event()
    real_lock = twitch.prompt_claim_pass_lock
    real_lock.acquire()

    class PassLock:
        # Another claim pass holds the lock.
        def acquire(self, timeout=-1):
            waited.set()
            return real_lock.acquire(timeout=timeout)

        def release(self):
            real_lock.release()

    twitch.prompt_claim_pass_lock = PassLock()
    try:
        start_event_claim(twitch)
        assert waited.wait(5)
        twitch.running = False
        assert twitch.join_drop_claim_workers(timeout=5) == []
    finally:
        real_lock.release()
    assert claimed(twitch) == []


def test_a_claim_thread_that_gets_the_lock_as_the_miner_stops_claims_nothing(
    twitch,
):
    class PassLock:
        # The claim pass ends just as the miner stops.
        def acquire(self, timeout=-1, blocking=True):
            twitch.running = False
            return True

        def release(self):
            pass

    twitch.prompt_claim_pass_lock = PassLock()
    start_event_claim(twitch)
    start_prompt_claim(twitch)
    assert twitch.join_drop_claim_workers(timeout=5) == []
    assert claimed(twitch) == []


def test_a_claim_twitch_is_answering_is_finished_before_shutdown_goes_on(twitch):
    answering = Event()
    answer = Event()

    def claim_all_drops_from_inventory():
        answering.set()
        assert answer.wait(5)
        claimed(twitch).append("claim")

    with patch.object(
        Twitch,
        "claim_all_drops_from_inventory",
        lambda self: claim_all_drops_from_inventory(),
    ):
        start_event_claim(twitch)
        assert answering.wait(5)
        twitch.running = False
        # Not finished within the wait: still running, named.
        assert twitch.join_drop_claim_workers(timeout=0.05) == ["Drop claim (PubSub)"]
        answer.set()
        assert twitch.join_drop_claim_workers(timeout=5) == []
    assert claimed(twitch) == ["claim"]


def test_the_inventory_pass_claims_nothing_once_the_miner_stops(twitch, monkeypatch):
    monkeypatch.setattr(Twitch, "claim_all_drops_from_inventory", CLAIM_ALL_DROPS)
    drops = [
        {
            "id": f"drop-{index}",
            "name": "Reward",
            "requiredMinutesWatched": 60,
            "startAt": "2020-01-01T00:00:00Z",
            "endAt": "2099-01-01T00:00:00Z",
            "self": {
                "currentMinutesWatched": 60,
                "dropInstanceID": f"i-{index}",
                "isClaimed": False,
                "hasPreconditionsMet": True,
            },
        }
        for index in (1, 2)
    ]
    monkeypatch.setattr(
        Twitch,
        "_Twitch__get_inventory",
        lambda self: {
            "dropCampaignsInProgress": [
                {"id": "c1", "name": "Campaign", "game": {}, "timeBasedDrops": drops}
            ]
        },
    )
    monkeypatch.setattr(
        Twitch, "_Twitch__campaign_link_from_inventory", lambda self, campaign: None
    )
    waits = []
    twitch.drop_claim_stop_event = SimpleNamespace(
        wait=lambda seconds: waits.append(seconds), is_set=lambda: False
    )
    claims = []

    def claim_drop(self, drop, **kwargs):
        claims.append(drop.drop_instance_id)
        self.claim_requests_sent = getattr(self, "claim_requests_sent", 0) + 1
        # The miner stops while Twitch answers this claim.
        self.running = False
        return True

    monkeypatch.setattr(Twitch, "claim_drop", claim_drop)
    monkeypatch.setattr(Twitch, "_Twitch__log_drop_check", lambda self, *a, **k: None)
    twitch.claim_all_drops_from_inventory()
    assert claims == ["i-1"]
    # The pause between claims waits on the stop event, which a stop wakes.
    assert len(waits) == 1
    twitch.drop_claim_stop_event = Event()


def test_a_new_twitch_after_a_stopped_one_claims(twitch, monkeypatch):
    # The desktop app stops a miner and starts another in the same process.
    twitch.running = False
    assert twitch.join_drop_claim_workers(timeout=5) == []
    other = object.__new__(Twitch)
    other.running = True
    other.prompt_claim_lock = Lock()
    other.prompt_claim_pass_lock = Lock()
    other.prompt_claim_last = {}
    other.drop_claim_stop_event = Event()
    start_event_claim(other)
    finish(other)
    start_prompt_claim(other)
    finish(other)
    assert claimed(other) == ["claim", "claim"]
    assert other.join_drop_claim_workers(timeout=5) == []


# -- shutdown ---------------------------------------------------------------------
def stopping_miner(twitch):
    miner = object.__new__(TwitchChannelPointsMiner)
    miner.streamers = []
    miner.running = True
    miner.twitch = twitch
    miner.ws_pool = None
    miner.minute_watcher_thread = None
    miner.sync_campaigns_thread = None
    miner.drop_badge_catalog_thread = None
    miner.drop_badge_catalog_stop_event = Event()
    return miner


def test_shutdown_waits_for_claim_threads_before_the_journal_stops(twitch):
    events = []
    answering = Event()

    def claim_all_drops_from_inventory():
        answering.set()
        # Twitch answers after the stop began.
        twitch.drop_claim_stop_event.wait(5)
        events.append("claimed")

    miner = stopping_miner(twitch)
    with (
        patch.object(
            Twitch,
            "claim_all_drops_from_inventory",
            lambda self: claim_all_drops_from_inventory(),
        ),
        patch.object(
            TwitchChannelPointsMiner,
            "_TwitchChannelPointsMiner__stop_history_journal",
            side_effect=lambda: events.append("journal stopped"),
        ),
        patch.object(
            TwitchChannelPointsMiner,
            "_TwitchChannelPointsMiner__save_daily_report_state_safely",
            side_effect=lambda retire_all=False: events.append("saved"),
        ),
    ):
        start_event_claim(twitch)
        assert answering.wait(5)
        miner._TwitchChannelPointsMiner__stop_workers_now()
    assert events == ["saved", "claimed", "journal stopped", "saved"]
    assert twitch.running is False


def test_shutdown_goes_on_when_a_claim_thread_never_finishes(twitch):
    miner = stopping_miner(twitch)
    with (
        patch.object(
            Twitch,
            "join_drop_claim_workers",
            lambda self, timeout: ["Drop claim (PubSub)"],
        ),
        patch(f"{MINER_MODULE}.logger") as logger,
        patch.object(
            TwitchChannelPointsMiner, "_TwitchChannelPointsMiner__stop_history_journal"
        ),
        patch.object(
            TwitchChannelPointsMiner,
            "_TwitchChannelPointsMiner__save_daily_report_state_safely",
        ),
    ):
        miner._TwitchChannelPointsMiner__stop_workers_now()
    assert "Drop claim threads did not stop in time" in (
        logger.warning.call_args.args[0]
    )


def test_a_claim_thread_that_cannot_start_is_not_waited_for(twitch, monkeypatch):
    class Unstartable:
        def __init__(self, target, name, daemon):
            self.name = name

        def start(self):
            raise RuntimeError("can't start new thread")

        def join(self, timeout=None):
            raise RuntimeError("cannot join thread before it is started")

    monkeypatch.setattr(twitch_module, "Thread", Unstartable)
    start_event_claim(twitch)
    start_prompt_claim(twitch)
    assert twitch.drop_claim_threads == set()
    # The prompt claim did not run: not debounced.
    assert twitch.prompt_claim_last == {}
    twitch.running = False
    assert twitch.join_drop_claim_workers(timeout=5) == []


def test_a_claim_queued_behind_another_sends_nothing_after_a_stop(monkeypatch):
    # Waiting for the claim lock (another claim holds it) when the miner
    # stops: no new request once it gets the lock.
    twitch, drop, requests, _saved, _now = claim_recording_twitch(monkeypatch)
    real_lock = twitch._Twitch__claim_lock()

    class ClaimLock:
        def __enter__(self):
            twitch.running = False
            return real_lock.__enter__()

        def __exit__(self, *exc):
            return real_lock.__exit__(*exc)

    twitch.claim_drop_lock = ClaimLock()
    assert twitch.claim_drop(drop) is False
    assert requests == []
