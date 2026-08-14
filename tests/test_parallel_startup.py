import threading
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from types import SimpleNamespace

from TwitchChannelPointsMiner.TwitchChannelPointsMiner import _unique_streamer_names
from TwitchChannelPointsMiner.classes.Exceptions import (
    StreamerDoesNotExistException,
)
from TwitchChannelPointsMiner.classes.Twitch import Twitch
from TwitchChannelPointsMiner.classes.entities.Streamer import Streamer


class TwitchLoginStub:
    def __init__(self, error=None):
        self.user_id_loaded = False
        self.error = error

    def get_user_id(self):
        if self.error is not None:
            raise self.error
        self.user_id_loaded = True
        return 1234


def test_initialize_streamers_context_runs_work_in_parallel(monkeypatch):
    twitch = Twitch.__new__(Twitch)
    twitch.twitch_login = TwitchLoginStub()
    streamers = [Streamer(f"streamer{index}") for index in range(3)]
    barrier = threading.Barrier(len(streamers), timeout=5)
    initialized = []

    def load_channel_points_context(_twitch, streamer):
        assert twitch.twitch_login.user_id_loaded is True
        barrier.wait()
        initialized.append(("points", streamer.username))

    def check_streamer_online(_twitch, streamer):
        initialized.append(("online", streamer.username))

    monkeypatch.setattr(
        Twitch, "load_channel_points_context", load_channel_points_context
    )
    monkeypatch.setattr(Twitch, "check_streamer_online", check_streamer_online)
    monkeypatch.setattr(
        "TwitchChannelPointsMiner.classes.Twitch.random.uniform", lambda *_: 0
    )

    failed = twitch.initialize_streamers_context(streamers, max_workers=3)

    assert failed == set()
    assert {
        username for operation, username in initialized if operation == "points"
    } == {streamer.username for streamer in streamers}
    assert {
        username for operation, username in initialized if operation == "online"
    } == {streamer.username for streamer in streamers}


def test_channel_points_context_limits_parallel_requests():
    twitch = Twitch.__new__(Twitch)
    twitch.channel_points_semaphore = threading.BoundedSemaphore(3)
    state_lock = threading.Lock()
    three_started = threading.Event()
    release_requests = threading.Event()
    active_requests = 0
    max_active_requests = 0

    def get_channel_points_context(_username):
        nonlocal active_requests, max_active_requests
        with state_lock:
            active_requests += 1
            max_active_requests = max(max_active_requests, active_requests)
            if active_requests == 3:
                three_started.set()
        assert release_requests.wait(timeout=5)
        with state_lock:
            active_requests -= 1
        return SimpleNamespace(community=SimpleNamespace(channel=None))

    twitch.gql = SimpleNamespace(
        get_channel_points_context=get_channel_points_context
    )
    streamers = [Streamer(f"streamer{index}") for index in range(8)]

    with ThreadPoolExecutor(max_workers=len(streamers)) as executor:
        futures = [
            executor.submit(twitch.load_channel_points_context, streamer)
            for streamer in streamers
        ]
        assert three_started.wait(timeout=5)
        with state_lock:
            assert active_requests == 3
            assert max_active_requests == 3
        release_requests.set()
        for future in futures:
            future.result(timeout=5)

    assert max_active_requests == 3


def test_initialize_streamers_context_isolates_individual_failures(monkeypatch):
    twitch = Twitch.__new__(Twitch)
    twitch.twitch_login = TwitchLoginStub()
    streamers = [Streamer("valid"), Streamer("missing"), Streamer("broken")]
    checked_online = []

    def load_channel_points_context(_twitch, streamer):
        if streamer.username == "missing":
            raise StreamerDoesNotExistException(streamer.username)
        if streamer.username == "broken":
            raise RuntimeError("unexpected response")

    def check_streamer_online(_twitch, streamer):
        checked_online.append(streamer.username)

    monkeypatch.setattr(
        Twitch, "load_channel_points_context", load_channel_points_context
    )
    monkeypatch.setattr(Twitch, "check_streamer_online", check_streamer_online)
    monkeypatch.setattr(
        "TwitchChannelPointsMiner.classes.Twitch.random.uniform", lambda *_: 0
    )

    failed = twitch.initialize_streamers_context(streamers)

    assert failed == {"missing", "broken"}
    assert checked_online == ["valid"]


def test_initialize_streamers_context_accepts_an_empty_list():
    twitch = Twitch.__new__(Twitch)

    assert twitch.initialize_streamers_context([]) == set()


def test_initialize_streamers_context_falls_back_to_one_worker(monkeypatch):
    twitch = Twitch.__new__(Twitch)
    twitch.twitch_login = TwitchLoginStub(error=RuntimeError("network unavailable"))
    streamers = [Streamer("first"), Streamer("second")]
    worker_counts = []
    twitch_module = import_module("TwitchChannelPointsMiner.classes.Twitch")

    def recording_executor(*args, **kwargs):
        worker_counts.append(kwargs["max_workers"])
        return ThreadPoolExecutor(*args, **kwargs)

    monkeypatch.setattr(twitch_module, "ThreadPoolExecutor", recording_executor)
    monkeypatch.setattr(Twitch, "load_channel_points_context", lambda *_: None)
    monkeypatch.setattr(Twitch, "check_streamer_online", lambda *_: None)
    monkeypatch.setattr(
        "TwitchChannelPointsMiner.classes.Twitch.random.uniform", lambda *_: 0
    )

    assert twitch.initialize_streamers_context(streamers) == set()
    assert worker_counts == [1]


def test_unique_streamer_names_preserves_first_seen_order():
    assert _unique_streamer_names(["alpha", "beta", "alpha", "gamma", "beta"]) == [
        "alpha",
        "beta",
        "gamma",
    ]
