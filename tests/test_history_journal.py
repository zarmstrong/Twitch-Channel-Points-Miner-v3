"""The miner side of History: the journal writer and its record helpers."""

import json
import os
import threading
from types import SimpleNamespace

import pytest

from TwitchChannelPointsMiner.history import journal as journal_module
from TwitchChannelPointsMiner.history.journal import (
    ACK_FILE,
    RUN_COUNTER_FILE,
    WATCH_CREDIT_CAP,
    Journal,
    lifecycle_lock,
    list_segments,
    segment_name,
)

# Payload shapes copied from the miner's debug log, with made-up ids.
POINTS_EARNED = {
    "timestamp": "2026-11-23T07:01:25.601846985Z",
    "channel_id": "1001",
    "point_gain": {
        "user_id": "2002",
        "channel_id": "1001",
        "total_points": 10,
        "baseline_points": 10,
        "reason_code": "WATCH",
        "multipliers": [],
    },
    "balance": {"user_id": "2002", "channel_id": "1001", "balance": 960},
}
POINTS_SPENT = {
    "timestamp": "2026-11-18T21:26:47.483279752Z",
    "balance": {"user_id": "2002", "channel_id": "1001", "balance": 0},
}
REWARD_REDEEMED = {
    "timestamp": "2026-11-18T21:26:47.483279752Z",
    "redemption": {
        "id": "4629386b-0000-0000-0000-c429478b3f98",
        "user": {"id": "2002", "login": "viewer", "display_name": "Viewer"},
        "channel_id": "1001",
        "redeemed_at": "2026-11-18T21:26:47.483279752Z",
        "reward": {
            "id": "5c11208e-0000-0000-0000-a6a232a527ce",
            "channel_id": "1001",
            "title": "Yes Yes Yes",
            "cost": 1000,
        },
    },
}


def streamer(name="alice", online=True, broadcast="b1", **flags):
    values = dict(
        explicitly_configured=True,
        from_followers=False,
        from_category=False,
        from_badge_campaign=False,
        from_wildcard_category=False,
    )
    values.update(flags)
    return SimpleNamespace(
        username=name,
        is_online=online,
        stream=SimpleNamespace(
            broadcast_id=broadcast, game={"displayName": "Game"}, title="Title"
        ),
        **values,
    )


class Clock(object):
    def __init__(self):
        self.wall = 1_800_000_000.0
        self.mono = 100.0

    def tick(self, seconds):
        self.wall += seconds
        self.mono += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def journal(tmp_path, clock):
    writer = Journal(clock=lambda: clock.wall, monotonic=lambda: clock.mono)
    assert writer.start(str(tmp_path / "history"), version="1.0")
    yield writer
    writer.stop()


def records(folder):
    lines = []
    for name in list_segments(folder):
        with open(os.path.join(folder, name), "rb") as handle:
            for raw in handle.read().split(b"\n"):
                if raw:
                    try:
                        lines.append(json.loads(raw))
                    except ValueError:
                        lines.append(None)
    return lines


def kinds(folder, kind=None):
    found = [record for record in records(folder) if record is not None]
    if kind is None:
        return [record["kind"] for record in found]
    return [record for record in found if record["kind"] == kind]


def test_the_miner_imports_without_sqlite():
    """History is optional: a Python build without sqlite3 still mines."""

    import subprocess
    import sys

    code = (
        "import sys; sys.modules['sqlite3'] = None; sys.modules['_sqlite3'] = None; "
        "import TwitchChannelPointsMiner.TwitchChannelPointsMiner; "
        "import TwitchChannelPointsMiner.history as h; h.start_journal(); "
        "print('ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    assert result.stdout.strip() == "ok", result.stderr


# -- runs and segments -----------------------------------------------------
def test_each_start_is_a_new_run_numbered_after_the_last(tmp_path, clock):
    folder = str(tmp_path / "history")
    first = Journal(clock=lambda: clock.wall)
    first.start(folder)
    first.stop()
    second = Journal(clock=lambda: clock.wall)
    second.start(folder)
    second.stop()
    assert list_segments(folder) == [segment_name(1, 1), segment_name(2, 1)]
    with open(os.path.join(folder, RUN_COUNTER_FILE)) as handle:
        assert handle.read().strip() == "2"
    assert kinds(folder) == ["run_start", "run_end", "run_start", "run_end"]
    assert [record["seq"] for record in records(folder)] == [1, 2, 1, 2]


def test_a_lost_run_counter_continues_after_the_newest_segment(tmp_path, clock):
    folder = tmp_path / "history"
    folder.mkdir()
    (folder / segment_name(7, 3)).write_bytes(b"")
    (folder / RUN_COUNTER_FILE).write_text("garbage")
    writer = Journal(clock=lambda: clock.wall)
    writer.start(str(folder))
    assert writer.run == 8


def test_rotation_is_by_size_and_pruning_follows_the_acknowledgement(
    journal, monkeypatch
):
    monkeypatch.setattr(journal_module, "JOURNAL_SEGMENT_BYTES", 300)
    for number in range(20):
        journal.record_event(streamer(), "win", f"label {number}")
    names = list_segments(journal.folder)
    assert len(names) > 3
    with open(os.path.join(journal.folder, ACK_FILE), "w") as handle:
        handle.write(names[1] + "\n")
    for number in range(5):
        journal.record_event(streamer(), "win", f"more {number}")
    remaining = list_segments(journal.folder)
    assert names[0] not in remaining and names[1] not in remaining
    assert names[2] in remaining


def test_the_size_limit_deletes_the_oldest_unacknowledged_segments(
    journal, monkeypatch, caplog
):
    monkeypatch.setattr(journal_module, "JOURNAL_SEGMENT_BYTES", 300)
    monkeypatch.setattr(journal_module, "JOURNAL_MAX_BYTES", 1000)
    for number in range(40):
        journal.record_event(streamer(), "win", f"label {number}")
    total = sum(
        os.path.getsize(os.path.join(journal.folder, name))
        for name in list_segments(journal.folder)
    )
    assert total <= 1000 + 400
    assert segment_name(journal.run, 1) not in list_segments(journal.folder)
    assert "deleted its oldest records" in caplog.text


def test_a_partial_line_never_joins_the_next_record(journal):
    with journal._lock:
        path = journal._path_locked()
        journal._close_locked()
    with open(path, "ab") as handle:
        handle.write(b'{"v":1,"run":')
    journal.record_event(streamer(), "win", "after")
    lines = records(journal.folder)
    assert lines[-2] is None
    assert lines[-1]["kind"] == "event"


def test_a_failed_write_is_logged_and_never_raises(journal, monkeypatch, caplog):
    class Broken(object):
        def write(self, _data):
            raise OSError("disk full")

        def close(self):
            pass

    journal._handle = Broken()
    assert journal.append("event", streamer="alice") is False
    assert "Unable to write the History journal" in caplog.text
    # The next record reopens the segment.
    assert journal.append("event", streamer="alice") is True


def test_nothing_is_written_before_start_or_after_stop(tmp_path, clock):
    writer = Journal(clock=lambda: clock.wall)
    assert writer.append("event") is False
    writer.record_live(streamer())
    writer.start(str(tmp_path / "history"))
    writer.stop()
    writer.record_live(streamer())
    assert kinds(str(tmp_path / "history")) == ["run_start", "run_end"]


@pytest.mark.parametrize("held", ["_lock", "_watch_lock"])
def test_stop_from_a_signal_handler_cannot_deadlock(journal, monkeypatch, held):
    """Ctrl+C runs stop() on the thread it interrupted, which may hold a
    journal lock."""

    monkeypatch.setattr(journal_module, "STOP_LOCK_SECONDS", 0.05)
    lock = getattr(journal, held)
    assert lock.acquire(timeout=1)
    try:
        stopper = threading.Thread(target=journal.stop)
        stopper.start()
        stopper.join(timeout=5)
        assert not stopper.is_alive()
    finally:
        lock.release()
    # However it ended, the run takes no more records and is not current.
    assert journal.running is False
    assert journal.append("event", streamer="alice") is False
    journal.record_event(streamer(), "win")
    assert "event" not in kinds(journal.folder)
    # The next start begins a new run.
    assert journal.start(journal.folder)
    assert journal.running is True


def test_nothing_follows_run_end(journal):
    alice = streamer()
    stop = threading.Event()

    def write():
        while not stop.is_set():
            journal.record_event(alice, "win")

    writer = threading.Thread(target=write)
    writer.start()
    try:
        while len(kinds(journal.folder, "event")) < 20:
            pass
        journal.stop()
    finally:
        stop.set()
        writer.join(timeout=5)
    assert kinds(journal.folder)[-1] == "run_end"


def test_no_run_number_without_a_listing(tmp_path, clock, monkeypatch):
    """Runs on disk that cannot be listed could be numbered again."""

    folder = str(tmp_path / "history")
    first = Journal(clock=lambda: clock.wall)
    first.start(folder)
    first.stop()
    os.remove(os.path.join(folder, RUN_COUNTER_FILE))

    def unlistable(_path):
        raise PermissionError("denied")

    with monkeypatch.context() as patch:
        patch.setattr(journal_module.os, "listdir", unlistable)
        assert Journal(clock=lambda: clock.wall).start(folder) is False
    assert Journal(clock=lambda: clock.wall).start(folder) is True


def test_a_run_whose_first_segment_exists_does_not_start(tmp_path, clock):
    folder = str(tmp_path / "history")
    os.makedirs(folder)
    writer = Journal(clock=lambda: clock.wall)
    real = writer._next_run_number
    writer._next_run_number = lambda path: 1
    with open(os.path.join(folder, segment_name(1, 1)), "wb") as handle:
        handle.write(b"")
    assert writer.start(folder) is False
    writer._next_run_number = real


def test_position_is_the_end_of_the_current_segment(journal):
    journal.record_event(streamer(), "win")
    name, size = journal.position()
    assert name == segment_name(journal.run, 1)
    assert size == os.path.getsize(os.path.join(journal.folder, name))


def test_a_delete_cutoff_covers_what_a_failed_write_left(journal):
    """A write that failed after all but its newline leaves a whole record;
    the cutoff must lie past it, not at its start."""

    journal.record_event(streamer(), "win")
    path = os.path.join(journal.folder, segment_name(journal.run, 1))

    class Torn(object):
        def write(self, data):
            with open(path, "ab") as handle:
                handle.write(data[:-1])
            raise OSError("disk full")

        def close(self):
            pass

    journal._handle = Torn()
    assert journal.record_event(streamer(), "lost") is None
    torn_start = [
        start
        for start, line in _lines_with_offsets(path)
        if b'"label":null' in line and b'"lost"' in line
    ]
    name, cutoff = journal.position()
    assert torn_start and torn_start[0] < cutoff
    assert cutoff == os.path.getsize(path)


def test_without_a_writable_journal_there_is_no_cutoff(journal, monkeypatch):
    journal._close_locked()
    monkeypatch.setattr(journal, "_open_locked", lambda: False)
    with pytest.raises(OSError):
        journal.position()


def _lines_with_offsets(path):
    with open(path, "rb") as handle:
        data = handle.read()
    position = 0
    while position < len(data):
        newline = data.find(b"\n", position)
        end = len(data) if newline < 0 else newline
        yield position, data[position:end]
        position = end + 1


# -- watch time --------------------------------------------------------------
def test_a_success_credits_only_the_time_since_the_previous_attempt(journal, clock):
    alice = streamer()
    journal.watch_selection({"alice": (alice, "points")})
    clock.tick(20)
    journal.watch_attempt(alice, False)
    clock.tick(20)
    journal.watch_attempt(alice, False)
    clock.tick(20)
    journal.watch_attempt(alice, True)
    watches = kinds(journal.folder, "watch")
    assert [record["seconds"] for record in watches] == [20.0]
    assert watches[0]["reason"] == "points"
    assert watches[0]["broadcast_id"] == "b1"


def test_skipped_requests_are_never_credited(journal, clock):
    """#393: time while the spade URL was missing is not watch time."""

    alice = streamer()
    journal.watch_selection({"alice": (alice, "points")})
    for _ in range(10):
        clock.tick(20)
        journal.watch_attempt(alice, False)
    clock.tick(5)
    journal.watch_attempt(alice, True)
    assert [record["seconds"] for record in kinds(journal.folder, "watch")] == [5.0]


def test_one_success_credits_at_most_the_cap(journal, clock):
    alice = streamer()
    journal.watch_selection({"alice": (alice, "points")})
    clock.tick(10_000)
    journal.watch_attempt(alice, True)
    assert kinds(journal.folder, "watch")[0]["seconds"] == WATCH_CREDIT_CAP


def test_time_before_entering_the_selection_is_not_credited(journal, clock):
    alice = streamer()
    clock.tick(500)
    journal.watch_attempt(alice, True)
    journal.watch_selection({"alice": (alice, "streak")})
    clock.tick(15)
    journal.watch_attempt(alice, True)
    assert [record["seconds"] for record in kinds(journal.folder, "watch")] == [15.0]


def test_leaving_the_selection_writes_watch_end(journal, clock):
    alice = streamer()
    journal.watch_selection({"alice": (alice, "points")})
    journal.watch_selection({})
    assert kinds(journal.folder, "watch_end") == [
        {**kinds(journal.folder, "watch_end")[0], "streamer": "alice"}
    ]
    clock.tick(20)
    journal.watch_attempt(alice, True)
    assert kinds(journal.folder, "watch") == []


def test_a_new_broadcast_resets_the_boundary(journal, clock):
    alice = streamer()
    journal.watch_selection({"alice": (alice, "points")})
    clock.tick(20)
    journal.watch_attempt(alice, True, broadcast_id="b1")
    clock.tick(20)
    journal.watch_attempt(alice, True, broadcast_id="b2")
    clock.tick(20)
    journal.watch_attempt(alice, True, broadcast_id="b2")
    assert [
        (record["broadcast_id"], record["seconds"])
        for record in kinds(journal.folder, "watch")
    ] == [("b1", 20.0), ("b2", 20.0)]


def test_a_new_broadcast_starts_its_watch_time_when_the_stream_changes(journal, clock):
    alice = streamer()
    journal.watch_selection({"alice": (alice, "points")})
    clock.tick(20)
    journal.watch_attempt(alice, True, broadcast_id="b1")
    clock.tick(10)
    alice.stream.broadcast_id = "b2"
    journal.record_live(alice)
    clock.tick(10)
    journal.watch_attempt(alice, True, broadcast_id="b2")
    assert [
        (record["broadcast_id"], record["seconds"])
        for record in kinds(journal.folder, "watch")
    ] == [("b1", 20.0), ("b2", 10.0)]


def test_a_request_for_an_ended_broadcast_changes_nothing(journal, clock):
    alice = streamer()
    journal.watch_selection({"alice": (alice, "points")})
    alice.stream.broadcast_id = "b2"
    journal.record_live(alice)
    clock.tick(5)
    # Sent before the change, answered after it.
    journal.watch_attempt(alice, True, broadcast_id="b1")
    clock.tick(5)
    journal.watch_attempt(alice, True, broadcast_id="b2")
    assert [
        (record["broadcast_id"], record["seconds"])
        for record in kinds(journal.folder, "watch")
    ] == [("b2", 10.0)]


def test_a_request_answered_after_offline_credits_nothing(journal, clock):
    alice = streamer()
    journal.watch_selection({"alice": (alice, "points")})
    clock.tick(20)
    token = journal.watch_token(alice)
    journal.record_offline(alice)
    clock.tick(5)
    journal.watch_attempt(alice, True, token=token)
    assert kinds(journal.folder, "watch") == []


def test_a_request_from_before_a_flap_credits_nothing(journal, clock):
    """Offline and back on the same broadcast before Twitch answered."""

    alice = streamer()
    journal.watch_selection({"alice": (alice, "points")})
    clock.tick(20)
    stale = journal.watch_token(alice)
    journal.record_offline(alice)
    journal.record_live(alice)
    journal.watch_selection({"alice": (alice, "points")})
    clock.tick(5)
    journal.watch_attempt(alice, True, token=stale)
    clock.tick(5)
    journal.watch_attempt(alice, True, token=journal.watch_token(alice))
    assert [record["seconds"] for record in kinds(journal.folder, "watch")] == [10.0]


def test_watching_restarts_when_live_again_before_the_next_selection(journal, clock):
    """A broadcast ended and a new one started inside one watch cycle: the
    new broadcast's minutes are credited without waiting for a selection."""

    alice = streamer()
    journal.watch_selection({"alice": (alice, "points")})
    clock.tick(30)
    alice.is_online = False
    journal.record_offline(alice)
    clock.tick(30)
    alice.is_online = True
    alice.stream.broadcast_id = "b2"
    journal.record_live(alice)
    clock.tick(20)
    journal.watch_attempt(alice, True, token=journal.watch_token(alice))
    assert [
        (record["broadcast_id"], record["seconds"])
        for record in kinds(journal.folder, "watch")
    ] == [("b2", 20.0)]


def test_nothing_is_credited_while_offline_and_still_selected(journal, clock):
    alice = streamer()
    journal.watch_selection({"alice": (alice, "points")})
    clock.tick(30)
    alice.is_online = False
    journal.record_offline(alice)
    journal.watch_selection({"alice": (alice, "points")})
    clock.tick(30)
    journal.watch_attempt(alice, True, token=journal.watch_token(alice))
    assert kinds(journal.folder, "watch") == []


def test_a_selection_restarts_watching_once_back_online(journal, clock):
    alice = streamer()
    journal.watch_selection({"alice": (alice, "points")})
    alice.is_online = False
    journal.record_offline(alice)
    clock.tick(100)
    alice.is_online = True
    journal.watch_selection({"alice": (alice, "points")})
    clock.tick(15)
    journal.watch_attempt(alice, True, token=journal.watch_token(alice))
    assert [record["seconds"] for record in kinds(journal.folder, "watch")] == [15.0]


@pytest.mark.parametrize("same_object", [True, False], ids=["same", "replacement"])
def test_time_while_removed_is_not_credited_when_added_back(
    journal, clock, same_object
):
    alice = streamer()
    journal.watch_selection({"alice": (alice, "points")})
    journal.watch_attempt(alice, True, token=journal.watch_token(alice))
    stale = journal.watch_token(alice)
    clock.tick(5)
    journal.retire(alice)
    clock.tick(10)
    if same_object:
        journal.reinstate(alice)
    else:
        alice = streamer()
    journal.record_live(alice)
    journal.watch_attempt(alice, True, token=stale)  # sent before the removal
    journal.watch_selection({"alice": (alice, "points")})
    clock.tick(5)
    journal.watch_attempt(alice, True, token=journal.watch_token(alice))
    assert [record["seconds"] for record in kinds(journal.folder, "watch")] == [5.0]


def test_retiring_an_old_object_keeps_its_replacements_watch(journal, clock):
    old, new = streamer(), streamer()
    journal.watch_selection({"alice": (new, "points")})
    journal.retire(old)
    clock.tick(5)
    journal.watch_attempt(new, True, token=journal.watch_token(new))
    assert [record["seconds"] for record in kinds(journal.folder, "watch")] == [5.0]


def test_a_demoted_channel_promoted_again_starts_its_watch_again(journal, clock):
    alice = streamer()
    journal.watch_selection({"alice": (alice, "points")})
    clock.tick(5)
    journal.record_untracked(alice, force=True)
    clock.tick(10)
    journal.watch_selection({"alice": (alice, "points")})
    clock.tick(5)
    journal.watch_attempt(alice, True, token=journal.watch_token(alice))
    assert [record["seconds"] for record in kinds(journal.folder, "watch")] == [5.0]


def test_watch_time_before_a_history_delete_is_not_credited_after_it(journal, clock):
    alice = streamer()
    journal.watch_selection({"alice": (alice, "points")})
    clock.tick(30)
    journal.delete_cutoff("alice", lambda position: None)
    journal.watch_selection({"alice": (alice, "points")})
    clock.tick(5)
    journal.watch_attempt(alice, True, token=journal.watch_token(alice))
    assert [record["seconds"] for record in kinds(journal.folder, "watch")] == [5.0]


def test_a_watch_started_during_a_delete_is_kept(journal, clock):
    """Selected again and sent after the cutoff, answered after the delete
    finished: that time is after the delete."""

    alice = streamer()
    journal.watch_selection({"alice": (alice, "points")})
    clock.tick(30)
    journal.delete_cutoff("alice", lambda position: None)
    journal.watch_selection({"alice": (alice, "points")})
    token = journal.watch_token(alice)
    journal.forget("alice")  # the delete finishes
    clock.tick(5)
    journal.watch_attempt(alice, True, token=token)
    assert [record["seconds"] for record in kinds(journal.folder, "watch")] == [5.0]


def test_a_channel_offline_before_its_selection_is_not_credited(journal, clock):
    """Selected while online, offline before the selection was recorded."""

    alice = streamer()
    alice.is_online = False
    journal.record_offline(alice)  # no watch state yet
    journal.watch_selection({"alice": (alice, "points")})
    clock.tick(30)
    journal.watch_attempt(alice, True, token=journal.watch_token(alice))
    assert kinds(journal.folder, "watch") == []
    # Back live: watching starts then.
    alice.is_online = True
    alice.stream.broadcast_id = "b2"
    journal.record_live(alice)
    clock.tick(10)
    journal.watch_attempt(alice, True, token=journal.watch_token(alice))
    assert [record["seconds"] for record in kinds(journal.folder, "watch")] == [10.0]


def test_a_request_from_an_older_payload_does_not_move_the_watch_back(journal, clock):
    """Built from b1's payload, its token taken after the switch to b2."""

    alice = streamer()
    journal.watch_selection({"alice": (alice, "points")})
    alice.is_online = False
    journal.record_offline(alice)
    alice.is_online = True
    alice.stream.broadcast_id = "b2"
    journal.record_live(alice)
    token = journal.watch_token(alice)
    clock.tick(10)
    journal.watch_attempt(alice, False, broadcast_id="b1", token=token)
    for _ in range(2):
        clock.tick(20)
        journal.watch_attempt(
            alice, True, broadcast_id="b2", token=journal.watch_token(alice)
        )
    assert [
        (record["broadcast_id"], record["seconds"])
        for record in kinds(journal.folder, "watch")
    ] == [("b2", 20.0), ("b2", 20.0)]


def test_a_delete_whose_cutoff_cannot_be_saved_leaves_the_watch_alone(journal, clock):
    alice = streamer()
    journal.watch_selection({"alice": (alice, "points")})
    token = journal.watch_token(alice)
    clock.tick(30)

    def save(position):
        raise OSError("read-only")

    with pytest.raises(OSError):
        journal.delete_cutoff("alice", save)
    journal.watch_attempt(alice, True, token=token)
    assert [record["seconds"] for record in kinds(journal.folder, "watch")] == [30.0]


# -- points and spends ---------------------------------------------------------
def test_points_earned_records_the_amount_and_its_identity(journal):
    journal.record_points_message(streamer(), "points-earned", POINTS_EARNED)
    (record,) = kinds(journal.folder, "points")
    assert record["delta"] == 10
    assert record["balance"] == 960
    assert record["reason_code"] == "WATCH"
    assert record["msg_key"]
    assert record["upstream_at"] == pytest.approx(1795417285.601847)


def test_a_replayed_message_is_dropped_even_after_another_one(journal):
    later = dict(POINTS_EARNED, timestamp="2026-11-23T07:06:25Z")
    journal.record_points_message(streamer(), "points-earned", POINTS_EARNED)
    journal.record_points_message(streamer(), "points-earned", later)
    journal.record_points_message(streamer(), "points-earned", POINTS_EARNED)
    assert len(kinds(journal.folder, "points")) == 2


def test_the_same_message_gets_the_same_key_in_another_run(tmp_path, clock):
    folder = str(tmp_path / "history")
    keys = []
    for _ in range(2):
        writer = Journal(clock=lambda: clock.wall)
        writer.start(folder)
        writer.record_points_message(streamer(), "points-earned", POINTS_EARNED)
        writer.stop()
    keys = [record["msg_key"] for record in kinds(folder, "points")]
    assert len(keys) == 2 and keys[0] == keys[1]


@pytest.mark.parametrize("server_time", [1795417285, 1795417285.25, "1795417285"])
def test_a_numeric_server_time_is_twitchs_time(journal, server_time):
    data = {key: value for key, value in POINTS_EARNED.items() if key != "timestamp"}
    data["server_time"] = server_time
    journal.record_points_message(streamer(), "points-earned", data)
    (record,) = kinds(journal.folder, "points")
    assert record["upstream_at"] == float(server_time)
    assert record["msg_key"] is not None


def _two_broadcasts(online_b2):
    """b1 went online at 1000, b2 at ``online_b2``."""

    alice = streamer(broadcast="b2")
    alice.online_history = (("b1", 1000.0), ("b2", online_b2))
    return alice


@pytest.mark.parametrize(
    "sent_at, expected",
    [
        (5000.0, "b1"),  # a late award from the earlier broadcast
        (500.0, None),  # older than what the miner knows: unknown
        (99_999.0, "b2"),  # sent well after b2 went online
    ],
)
def test_a_points_message_belongs_to_the_broadcast_it_was_sent_on(
    journal, sent_at, expected
):
    alice = _two_broadcasts(online_b2=10_000.0)
    data = json.loads(json.dumps(POINTS_EARNED))
    data["timestamp"] = sent_at
    data["point_gain"]["reason_code"] = "WATCH_STREAK"
    journal.record_points_message(alice, "points-earned", data)
    (record,) = kinds(journal.folder, "points")
    assert record["broadcast_id"] == expected
    assert record["delta"] == 10


def test_an_award_during_a_broadcast_change_stays_with_its_broadcast(journal):
    """A refresh has moved the stream to b2 but not yet marked b2 online."""

    alice = streamer(broadcast="b2")
    alice.online_history = (("b1", 1000.0),)
    data = json.loads(json.dumps(POINTS_EARNED))
    data["timestamp"] = 5000.0
    data["point_gain"]["reason_code"] = "WATCH_STREAK"
    journal.record_points_message(alice, "points-earned", data)
    assert kinds(journal.folder, "points")[0]["broadcast_id"] == "b1"


def test_points_spent_notes_the_balance_without_an_amount(journal):
    journal.record_points_message(streamer(), "points-spent", POINTS_SPENT)
    (record,) = kinds(journal.folder, "points")
    assert record["delta"] is None
    assert record["balance"] == 0


def test_a_message_without_an_upstream_time_has_no_key(journal):
    data = dict(POINTS_EARNED)
    del data["timestamp"]
    journal.record_points_message(streamer(), "points-earned", data)
    journal.record_points_message(streamer(), "points-earned", data)
    assert [record["msg_key"] for record in kinds(journal.folder, "points")] == [
        None,
        None,
    ]


def test_a_redemption_is_a_spend_of_its_cost_once(journal):
    journal.record_redemption(streamer(), REWARD_REDEEMED)
    journal.record_redemption(streamer(), REWARD_REDEEMED)
    (record,) = kinds(journal.folder, "spend")
    assert record["amount"] == 1000
    assert record["source"] == "redemption"
    assert record["spend_key"] == "redemption:4629386b-0000-0000-0000-c429478b3f98"
    assert record["title"] == "Yes Yes Yes"


# -- lifecycle -----------------------------------------------------------------
def test_live_is_written_once_per_state(journal):
    alice = streamer()
    journal.record_live(alice)
    journal.record_live(alice)
    alice.stream.title = "New title"
    journal.record_live(alice)
    journal.record_offline(alice)
    journal.record_live(alice)
    assert kinds(journal.folder) == [
        "run_start",
        "live",
        "live",
        "offline",
        "live",
    ]


def test_an_offline_streamer_is_not_written_live(journal):
    journal.record_live(streamer(online=False))
    assert kinds(journal.folder, "live") == []


def test_drops_only_channels_write_nothing(journal, clock):
    farmed = streamer(explicitly_configured=False, from_category=True)
    journal.record_live(farmed)
    journal.record_points_message(farmed, "points-earned", POINTS_EARNED)
    journal.record_redemption(farmed, REWARD_REDEEMED)
    journal.watch_selection({"alice": (farmed, "drops")})
    clock.tick(20)
    journal.watch_attempt(farmed, True)
    journal.record_offline(farmed)
    assert kinds(journal.folder) == ["run_start"]


def test_a_configured_drops_channel_is_recorded(journal):
    journal.record_live(streamer(from_category=True))
    assert len(kinds(journal.folder, "live")) == 1


def test_demoting_a_kept_channel_writes_untracked(journal):
    alice = streamer(from_badge_campaign=True, from_category=True)
    journal.record_live(alice)
    with journal.eligibility_change(alice):
        alice.explicitly_configured = False
    assert kinds(journal.folder)[-1] == "untracked"


def test_promoting_an_online_drops_channel_writes_live(journal):
    alice = streamer(explicitly_configured=False, from_category=True)
    with journal.eligibility_change(alice):
        alice.explicitly_configured = True
    assert kinds(journal.folder) == ["run_start", "live"]


def test_a_removed_streamer_records_nothing_more(journal, clock):
    alice = streamer()
    journal.record_live(alice)
    journal.watch_selection({"alice": (alice, "points")})
    journal.retire(alice)
    # A stream refresh, a request and a message still in flight.
    alice.stream.title = "New title"
    journal.record_live(alice)
    clock.tick(20)
    journal.watch_attempt(alice, True)
    journal.record_points_message(alice, "points-earned", POINTS_EARNED)
    journal.record_offline(alice)
    assert kinds(journal.folder)[1:] == ["live", "untracked"]
    # Added again: a new object, recorded as usual.
    journal.record_live(streamer())
    assert kinds(journal.folder)[1:] == ["live", "untracked", "live"]


def test_every_removed_object_stays_removed(journal):
    """Removed, added again, removed again: callbacks may still hold either
    of the old objects."""

    first = streamer()
    journal.retire(first)
    second = streamer()
    journal.record_live(second)
    journal.retire(second)
    for old in (first, second):
        old.stream.title = f"Late {id(old)}"
        journal.record_live(old)
        journal.record_spend(old, "prediction", 10, f"prediction:{id(old)}")
    assert kinds(journal.folder)[1:] == ["untracked", "live", "untracked"]


def test_a_watch_record_cannot_slip_in_after_a_removal(journal, clock):
    alice = streamer()
    journal.watch_selection({"alice": (alice, "points")})
    clock.tick(20)
    with lifecycle_lock("alice"):
        # The miner is removing the channel; a request finishes meanwhile.
        attempt = threading.Thread(target=journal.watch_attempt, args=(alice, True))
        attempt.start()
        attempt.join(timeout=0.2)
        assert attempt.is_alive()
        journal.retire(alice)
    attempt.join(timeout=5)
    assert "watch" not in kinds(journal.folder)


def test_a_lifecycle_change_and_its_record_cannot_interleave(journal):
    """A thread that changes the state under the lifecycle lock is waited
    for, so the journal order matches the order of the changes."""

    alice = streamer(online=False)
    inside = threading.Event()
    release = threading.Event()

    def go_online():
        with lifecycle_lock("alice"):
            alice.is_online = True
            inside.set()
            release.wait(timeout=5)
            journal.record_live(alice)

    first = threading.Thread(target=go_online)
    first.start()
    assert inside.wait(timeout=5)

    def go_offline():
        with lifecycle_lock("alice"):
            alice.is_online = False
            journal.record_offline(alice)

    second = threading.Thread(target=go_offline)
    second.start()
    second.join(timeout=0.2)
    assert second.is_alive()
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)
    assert kinds(journal.folder)[-2:] == ["live", "offline"]
