import threading

import pytest

from TwitchChannelPointsMiner.classes.CopyOnWriteList import CopyOnWriteList
from TwitchChannelPointsMiner.classes.WebSocketsPool import WebSocketsPool


def test_reads_behave_like_a_list():
    items = CopyOnWriteList(["a", "b", "c"])

    assert list(items) == ["a", "b", "c"]
    assert len(items) == 3
    assert items[1] == "b"
    assert items[-1] == "c"
    assert items[1:] == ["b", "c"]
    assert "b" in items
    assert items.index("c") == 2
    assert items.count("a") == 1
    assert items == ["a", "b", "c"]
    assert list(reversed(items)) == ["c", "b", "a"]
    assert bool(CopyOnWriteList()) is False


def test_writes_behave_like_a_list():
    items = CopyOnWriteList([3, 1, 2])

    items.append(4)
    items.extend([5])
    items.insert(0, 0)
    items[1] = 30
    items.remove(2)
    assert items.pop() == 5
    del items[0]
    items += [6]
    assert items == [30, 1, 4, 6]

    items.sort()
    assert items == [1, 4, 6, 30]
    items.sort(key=lambda value: -value)
    assert items == [30, 6, 4, 1]
    items.reverse()
    assert items == [1, 4, 6, 30]

    items[:] = (value for value in [7, 8])
    assert items == [7, 8]
    items.clear()
    assert items == []


def test_iteration_keeps_the_version_it_started_with():
    # Retirement used to shrink the shared list under a running loop, which
    # skipped streamers or raised in readers that indexed by position.
    items = CopyOnWriteList(["alice", "bob", "carol"])
    seen = []

    for item in items:
        seen.append(item)
        if item == "alice":
            items[:] = ["carol"]

    assert seen == ["alice", "bob", "carol"]
    assert items == ["carol"]


def test_a_snapshot_is_not_changed_by_later_writes():
    items = CopyOnWriteList(["alice", "bob"])
    snapshot = items.snapshot()

    items.pop()

    assert snapshot == ("alice", "bob")
    assert items == ["alice"]


def test_index_errors_match_a_list():
    with pytest.raises(IndexError):
        CopyOnWriteList()[0]
    with pytest.raises(ValueError):
        CopyOnWriteList(["a"]).remove("b")


def test_concurrent_appends_are_not_lost():
    items = CopyOnWriteList()

    def add(start):
        for value in range(start, start + 500):
            items.append(value)

    threads = [threading.Thread(target=add, args=(n * 500,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(items) == list(range(2000))


def test_readers_never_fail_while_another_thread_retires_streamers():
    items = CopyOnWriteList(range(200))
    stop = threading.Event()
    errors = []

    def read():
        while not stop.is_set():
            try:
                for value in items:
                    pass
                snapshot = items.snapshot()
                for position in range(len(snapshot)):
                    snapshot[position]
            except Exception as error:  # pragma: no cover - the failure case
                errors.append(error)
                return

    reader = threading.Thread(target=read)
    reader.start()
    try:
        for _ in range(200):
            items[:] = list(range(200))
            items[:] = [value for value in items if value % 2]
    finally:
        stop.set()
        reader.join(timeout=10)

    assert errors == []


def test_websocket_pool_list_is_copy_on_write():
    pool = WebSocketsPool(twitch=None, streamers=[], events_predictions={})

    assert isinstance(pool.ws, CopyOnWriteList)
