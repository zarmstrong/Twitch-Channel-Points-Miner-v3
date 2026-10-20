import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from TwitchChannelPointsMiner.classes.Chat import ChatPresence
from TwitchChannelPointsMiner.classes.Exceptions import StreamerDoesNotExistException
from TwitchChannelPointsMiner.classes.Settings import Events
from TwitchChannelPointsMiner.classes.WebSocketsPool import WebSocketsPool
from TwitchChannelPointsMiner.TwitchChannelPointsMiner import (
    TwitchChannelPointsMiner,
    _exit_after_crash,
)

MODULE = "TwitchChannelPointsMiner.TwitchChannelPointsMiner"
POOL_MODULE = "TwitchChannelPointsMiner.classes.WebSocketsPool"


def _miner():
    miner = TwitchChannelPointsMiner.__new__(TwitchChannelPointsMiner)
    miner.running = True
    miner.streamers = []
    miner.twitch = SimpleNamespace(running=True)
    miner.drop_badge_catalog_stop_event = threading.Event()
    miner.ws_pool = None
    miner.minute_watcher_thread = None
    miner.sync_campaigns_thread = None
    miner.drop_badge_catalog_thread = None
    miner.queue_listener = MagicMock()
    return miner


def _streamer(username, is_online=True):
    return SimpleNamespace(
        username=username, is_online=is_online, irc_chat=None, mutex=MagicMock()
    )


def test_channel_points_refresh_survives_streamers_retired_mid_loop():
    # Reproduces #169: the badge Drop check retired streamers by shrinking
    # self.streamers in place while the main loop iterated it by index, which
    # raised IndexError and killed run().
    miner = _miner()
    miner.streamers = [_streamer("alice"), _streamer("bob"), _streamer("carol")]
    refreshed = []

    def load_channel_points_context(streamer):
        refreshed.append(streamer.username)
        if streamer.username == "alice":
            miner.streamers[:] = miner.streamers[:1]

    miner.twitch.load_channel_points_context = load_channel_points_context

    miner._TwitchChannelPointsMiner__refresh_channel_points_context()

    # bob and carol were retired mid-loop, so they are skipped, not refreshed.
    assert refreshed == ["alice"]


def test_channel_points_refresh_survives_a_streamer_that_no_longer_exists():
    # A banned or renamed channel raised StreamerDoesNotExistException out of
    # the 30-minute refresh and took the whole main loop down with it.
    miner = _miner()
    miner.streamers = [_streamer("gone"), _streamer("bob"), _streamer("carol")]
    refreshed = []

    def load_channel_points_context(streamer):
        if streamer.username == "gone":
            raise StreamerDoesNotExistException
        if streamer.username == "bob":
            raise RuntimeError("GQL hiccup")
        refreshed.append(streamer.username)

    miner.twitch.load_channel_points_context = load_channel_points_context

    with patch(f"{MODULE}.logger") as logger:
        miner._TwitchChannelPointsMiner__refresh_channel_points_context()

    assert refreshed == ["carol"]
    logger.warning.assert_called_once()


def test_channel_points_refresh_skips_offline_streamers():
    miner = _miner()
    miner.streamers = [_streamer("alice"), _streamer("bob", is_online=False)]
    miner.twitch.load_channel_points_context = MagicMock()

    miner._TwitchChannelPointsMiner__refresh_channel_points_context()

    miner.twitch.load_channel_points_context.assert_called_once_with(miner.streamers[0])


def test_websocket_check_survives_websockets_retired_mid_loop():
    first = SimpleNamespace(
        index=0, is_reconnecting=False, elapsed_last_ping=lambda: 11
    )
    second = SimpleNamespace(
        index=1, is_reconnecting=False, elapsed_last_ping=lambda: 11
    )
    miner = _miner()
    miner.ws_pool = SimpleNamespace(ws=[first, second])
    reconnected = []

    def handle_reconnection(websocket):
        reconnected.append(websocket)
        miner.ws_pool.ws.pop()

    with patch(f"{MODULE}.internet_connection_available", return_value=True), patch(
        f"{MODULE}.WebSocketsPool.handle_reconnection",
        side_effect=handle_reconnection,
    ):
        miner._TwitchChannelPointsMiner__check_websockets()

    assert reconnected == [first, second]


def test_mine_stops_workers_and_exits_non_zero_when_run_crashes():
    # A crash in run() used to leave the non-daemon worker threads running,
    # so the process stayed up as a zombie and a container restart policy
    # never recovered it.
    miner = _miner()
    miner.streamers = [_streamer("alice")]
    miner.ws_pool = MagicMock()
    minute_watcher = MagicMock()
    minute_watcher.is_alive.side_effect = [True, False]
    miner.minute_watcher_thread = minute_watcher

    with patch.object(
        TwitchChannelPointsMiner, "run", side_effect=IndexError("boom")
    ), patch(f"{MODULE}.logger") as logger:
        with pytest.raises(SystemExit) as exit_info:
            miner.mine([])
    assert exit_info.value.code == 1

    assert miner.running is False
    assert miner.twitch.running is False
    assert miner.drop_badge_catalog_stop_event.is_set()
    miner.ws_pool.end.assert_called_once()
    minute_watcher.join.assert_called_once()
    miner.queue_listener.stop.assert_called_once()
    logger.error.assert_called_once()
    extra = logger.error.call_args.kwargs["extra"]
    assert extra["force_alert"] is True
    assert extra["event"] is Events.CONFIGURATION
    assert logger.error.call_args.kwargs["exc_info"] is True


def test_mine_does_not_stop_workers_on_normal_return():
    miner = _miner()
    miner.ws_pool = MagicMock()

    with patch.object(TwitchChannelPointsMiner, "run"):
        miner.mine([])

    assert miner.running is True
    miner.ws_pool.end.assert_not_called()
    miner.queue_listener.stop.assert_not_called()


def test_mine_keeps_the_original_error_when_a_worker_was_never_started():
    # run() creates the badge catalog thread well before starting it; a crash
    # in between must not turn into "cannot join thread before it is started".
    miner = _miner()
    miner.drop_badge_catalog_thread = threading.Thread(target=lambda: None)

    with patch.object(
        TwitchChannelPointsMiner, "run", side_effect=IndexError("boom")
    ), patch(f"{MODULE}.logger"):
        with pytest.raises(SystemExit) as exit_info:
            miner.mine([])
    assert exit_info.value.code == 1

    miner.queue_listener.stop.assert_called_once()


def test_signal_during_crash_shutdown_does_not_exit_cleanly():
    # A SIGTERM handled while the crash handler is still stopping workers used
    # to run end() again and sys.exit(0), masking the failure exit code.
    miner = _miner()
    streamer = _streamer("alice")
    streamer.irc_chat = MagicMock()
    streamer.irc_chat.is_alive.return_value = False
    streamer.settings = SimpleNamespace(chat=ChatPresence.ALWAYS)
    # Leaving chat is the first, slowest step of stopping the workers.
    streamer.leave_chat = lambda: miner.end(None, None)
    miner.streamers = [streamer]

    with patch.object(
        TwitchChannelPointsMiner, "run", side_effect=IndexError("boom")
    ), patch(f"{MODULE}.logger"):
        with pytest.raises(SystemExit) as exit_info:
            miner.mine([])
    assert exit_info.value.code == 1

    miner.queue_listener.stop.assert_called_once()


def _pool(websockets):
    pool = WebSocketsPool(
        SimpleNamespace(twitch_login=SimpleNamespace(get_auth_token=lambda: "t")),
        [],
        {},
    )
    pool.ws = websockets
    return pool


def test_reconnection_skips_a_websocket_retired_while_sleeping(monkeypatch):
    # remove_streamer_topics can pop a websocket during handle_reconnection's
    # sleep; writing the replacement back to its old slot raised IndexError.
    websocket = SimpleNamespace(
        index=1,
        is_reconnecting=False,
        forced_close=False,
        topics=[],
        is_closed=False,
        keep_running=True,
    )
    pool = _pool([SimpleNamespace(index=0), websocket])
    websocket.parent_pool = pool
    new = MagicMock()
    monkeypatch.setattr(WebSocketsPool, "_WebSocketsPool__new", new)
    monkeypatch.setattr(
        f"{POOL_MODULE}.time.sleep", lambda _seconds: pool.ws.pop()
    )
    monkeypatch.setattr(f"{POOL_MODULE}.internet_connection_available", lambda: True)

    WebSocketsPool.handle_reconnection(websocket)

    new.assert_not_called()
    assert len(pool.ws) == 1


def test_pool_end_survives_websockets_retired_mid_loop():
    closed = []

    def websocket(index):
        ws = SimpleNamespace(index=index, forced_close=False)

        def close():
            closed.append(ws.index)
            if ws.index == 0:
                pool.ws.pop()

        ws.close = close
        return ws

    pool = _pool([])
    pool.ws = [websocket(0), websocket(1)]

    pool.end()

    assert closed == [0, 1]


def test_reconnection_replay_skips_a_replacement_retired_while_sleeping(monkeypatch):
    # The replacement can itself be retired during the sleep before topics are
    # replayed; replaying onto its old slot raised IndexError.
    topic = SimpleNamespace(name="topic")
    websocket = SimpleNamespace(
        index=1,
        is_reconnecting=False,
        forced_close=False,
        topics=[topic],
        is_closed=False,
        keep_running=True,
    )
    pool = _pool([SimpleNamespace(index=0), websocket])
    websocket.parent_pool = pool
    replacement = SimpleNamespace(index=1, topics=[], pending_topics=[])
    started = []
    sleeps = []
    monkeypatch.setattr(
        WebSocketsPool, "_WebSocketsPool__new", lambda _self, _index: replacement
    )
    monkeypatch.setattr(
        WebSocketsPool,
        "_WebSocketsPool__start",
        lambda _self, index: started.append(_self.ws[index]),
    )
    register = MagicMock()
    monkeypatch.setattr(WebSocketsPool, "_WebSocketsPool__register", register)

    def sleep(_seconds):
        sleeps.append(_seconds)
        if len(sleeps) == 2:
            pool.ws.pop()

    monkeypatch.setattr(f"{POOL_MODULE}.time.sleep", sleep)
    monkeypatch.setattr(f"{POOL_MODULE}.internet_connection_available", lambda: True)

    WebSocketsPool.handle_reconnection(websocket)

    assert started == [replacement]
    register.assert_not_called()


def test_report_survives_streamers_retired_while_it_runs():
    # end() prints the report while the badge catalog thread may still be
    # retiring streamers; pairing the two lists by index raised IndexError.
    miner = _miner()
    miner.config_reload_lock = threading.RLock()
    miner.session_id = "session"
    miner.logs_file = None
    miner.start_datetime = datetime.now()
    miner.events_predictions = {}
    miner.original_drop_progress = {}
    miner.twitch.drop_report_snapshot = lambda: {}

    def streamer(username, points):
        streamer = _streamer(username)
        streamer.channel_points = points
        streamer.history = {"WATCH": 1}
        streamer.print_history = lambda: "WATCH(1 times)"
        return streamer

    miner.streamers = [streamer("alice", 150), streamer("bob", 260)]
    miner.original_streamers = [100, 200]
    gains = []

    def info(message, *args, **kwargs):
        if kwargs.get("extra", {}).get("emoji") == ":moneybag:":
            gains.append(message)
            # A retirement lands while the report is printing.
            miner.streamers[:] = []
            miner.original_streamers[:] = []

    with patch(f"{MODULE}.logger") as logger, patch(
        f"{MODULE}._drop_progress_report_entries", return_value=[]
    ):
        logger.info.side_effect = info
        # Held by this same thread, as when end() runs as a signal handler in
        # the middle of a category refresh; must not stall or deadlock.
        with miner.config_reload_lock:
            miner._TwitchChannelPointsMiner__print_report()

    assert len(gains) == 2
    assert "alice" in gains[0] and "50" in gains[0]
    assert "bob" in gains[1] and "60" in gains[1]


def test_crash_alert_is_flushed_even_if_stopping_a_worker_fails():
    miner = _miner()
    miner.ws_pool = MagicMock()
    miner.ws_pool.end.side_effect = OSError("socket already gone")

    with patch.object(
        TwitchChannelPointsMiner, "run", side_effect=IndexError("boom")
    ), patch(f"{MODULE}.logger"):
        with pytest.raises(SystemExit) as exit_info:
            miner.mine([])
    assert exit_info.value.code == 1

    miner.queue_listener.stop.assert_called_once()


def test_stop_workers_waits_for_the_running_irc_thread():
    # leave_chat() swaps in a fresh, unstarted ThreadChat; joining
    # streamer.irc_chat afterwards never waited for the real IRC thread.
    miner = _miner()
    running_chat = MagicMock()
    running_chat.is_alive.return_value = True
    streamer = _streamer("alice")
    streamer.irc_chat = running_chat
    streamer.settings = SimpleNamespace(chat=ChatPresence.ALWAYS)
    streamer.mutex.locked.return_value = False

    def leave_chat():
        streamer.irc_chat = MagicMock()

    streamer.leave_chat = leave_chat
    miner.streamers = [streamer]

    miner._TwitchChannelPointsMiner__stop_workers()

    running_chat.join.assert_called_once_with(timeout=10)


def _lingering_thread():
    release = threading.Event()
    thread = threading.Thread(target=release.wait, name="stuck worker")
    thread.start()
    return thread, release


def test_exit_after_crash_forces_exit_when_a_worker_is_stuck():
    # Interpreter shutdown joins non-daemon threads without a timeout, so a
    # stuck worker would otherwise keep the crashed process up.
    thread, release = _lingering_thread()
    try:
        with patch(f"{MODULE}.os._exit", side_effect=RuntimeError("exited")) as (
            exit_mock
        ), patch(f"{MODULE}.logger") as logger:
            with pytest.raises(RuntimeError, match="exited"):
                _exit_after_crash()
    finally:
        release.set()
        thread.join()

    exit_mock.assert_called_once_with(1)
    assert "stuck worker" in logger.warning.call_args.args[0]


def test_exit_after_crash_only_ends_the_thread_off_the_main_thread():
    # The Windows shell runs the miner off the main thread, where os._exit
    # would kill the whole app.
    thread, release = _lingering_thread()
    outcome = []

    def crash():
        try:
            _exit_after_crash()
        except SystemExit as error:
            outcome.append(error.code)

    try:
        with patch(f"{MODULE}.os._exit") as exit_mock:
            miner_thread = threading.Thread(target=crash)
            miner_thread.start()
            miner_thread.join()
    finally:
        release.set()
        thread.join()

    exit_mock.assert_not_called()
    assert outcome == [1]
