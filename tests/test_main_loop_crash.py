import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from TwitchChannelPointsMiner.classes.Chat import ChatPresence
from TwitchChannelPointsMiner.classes.Settings import Events
from TwitchChannelPointsMiner.classes.WebSocketsPool import WebSocketsPool
from TwitchChannelPointsMiner.TwitchChannelPointsMiner import TwitchChannelPointsMiner

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

    assert refreshed == ["alice", "bob", "carol"]


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


def test_mine_stops_workers_and_reraises_when_run_crashes():
    # A crash in run() used to leave the non-daemon worker threads running,
    # so the process stayed up as a zombie and a container restart policy
    # never recovered it.
    miner = _miner()
    miner.streamers = [_streamer("alice")]
    miner.ws_pool = MagicMock()
    minute_watcher = MagicMock()
    minute_watcher.is_alive.return_value = False
    miner.minute_watcher_thread = minute_watcher

    with patch.object(
        TwitchChannelPointsMiner, "run", side_effect=IndexError("boom")
    ), patch(f"{MODULE}.logger") as logger:
        with pytest.raises(IndexError, match="boom"):
            miner.mine([])

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
        with pytest.raises(IndexError, match="boom"):
            miner.mine([])

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
        with pytest.raises(IndexError, match="boom"):
            miner.mine([])

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
