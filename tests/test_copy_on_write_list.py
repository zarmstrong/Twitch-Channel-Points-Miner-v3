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


def test_miner_streamer_lists_are_copy_on_write(tmp_path, monkeypatch):
    import importlib
    import logging
    import signal
    import socket

    from TwitchChannelPointsMiner.logger import LoggerSettings

    monkeypatch.chdir(tmp_path)
    # The constructor waits until twitch.tv resolves; stay off the network.
    monkeypatch.setattr(socket, "gethostbyname", lambda host: "127.0.0.1")
    miner_module = importlib.import_module(
        "TwitchChannelPointsMiner.TwitchChannelPointsMiner"
    )
    root_logger = logging.getLogger()
    existing_handlers = set(root_logger.handlers)
    existing_level = root_logger.level
    signals = (signal.SIGINT, signal.SIGSEGV, signal.SIGTERM)
    existing_signal_handlers = {sign: signal.getsignal(sign) for sign in signals}
    try:
        miner = miner_module.TwitchChannelPointsMiner(
            "someone",
            update_check=False,
            logger_settings=LoggerSettings(save=False),
        )
        miner.queue_listener.stop()
        for handler in miner.queue_listener.handlers:
            handler.close()
    finally:
        # The miner configures the root logger and installs signal handlers;
        # undo both so later tests see the defaults.
        for handler in set(root_logger.handlers) - existing_handlers:
            root_logger.removeHandler(handler)
            handler.close()
        root_logger.setLevel(existing_level)
        for sign, handler in existing_signal_handlers.items():
            signal.signal(sign, handler)

    # run() refills these same objects, so they stay copy-on-write.
    assert isinstance(miner.streamers, CopyOnWriteList)
    assert isinstance(miner.original_streamers, CopyOnWriteList)
