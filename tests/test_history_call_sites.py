"""Where the miner writes History journal records (design section 5.3)."""

import json
import os
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests
from test_history_journal import POINTS_EARNED, POINTS_SPENT, REWARD_REDEEMED
from test_streamer import streamer_settings
from test_streams_watched import _run_one_watch_iteration, _watch_streamer
from test_websockets_pool_on_message import FakeStreamer, _pubsub, _websocket

from TwitchChannelPointsMiner.classes.entities.Streamer import Streamer
from TwitchChannelPointsMiner.classes.Twitch import Twitch
from TwitchChannelPointsMiner.classes.WebSocketsPool import WebSocketsPool
from TwitchChannelPointsMiner.history import journal as journal_module
from TwitchChannelPointsMiner.history.journal import Journal, list_segments
from TwitchChannelPointsMiner.TwitchChannelPointsMiner import (
    TwitchChannelPointsMiner,
)


@pytest.fixture
def journal(tmp_path, monkeypatch):
    clock = {"mono": 0.0}
    writer = Journal(monotonic=lambda: clock["mono"])
    writer.clock_state = clock
    writer.start(str(tmp_path / "history"))
    monkeypatch.setattr(journal_module, "JOURNAL", writer)
    yield writer
    writer.stop()


def written(journal, kind=None):
    found = []
    for name in list_segments(journal.folder):
        with open(os.path.join(journal.folder, name), "rb") as handle:
            for raw in handle.read().split(b"\n"):
                if raw:
                    found.append(json.loads(raw))
    found = [record for record in found if record["kind"] != "run_start"]
    if kind is None:
        return [record["kind"] for record in found]
    return [record for record in found if record["kind"] == kind]


# -- minute watcher ----------------------------------------------------------
def _watched(journal, monkeypatch, effect, streamer=None, configure=None):
    streamer = streamer or _watch_streamer("alice", explicitly_configured=True)
    streamer.stream.broadcast_id = "b1"
    if configure is not None:
        configure(streamer)
    # The selection starts the boundary; move time on before the request.
    real_selection = journal.watch_selection

    def selection(selected):
        real_selection(selected)
        journal.clock_state["mono"] += 20

    monkeypatch.setattr(journal, "watch_selection", selection)
    _run_one_watch_iteration(
        monkeypatch,
        [streamer],
        streams_watched=1,
        post_side_effects=effect if isinstance(effect, list) else [effect],
    )
    return written(journal, "watch")


def test_an_accepted_minute_watched_request_credits_watch_time(journal, monkeypatch):
    watches = _watched(journal, monkeypatch, SimpleNamespace(status_code=204))
    assert [(w["streamer"], w["seconds"], w["broadcast_id"]) for w in watches] == [
        ("alice", 20.0, "b1")
    ]


def test_a_retry_after_a_refresh_is_credited_to_the_new_broadcast(journal, monkeypatch):
    """The retry posts the refreshed payload; History credits what it sent."""

    real_attempt = journal.watch_attempt

    def attempt(streamer, success, **kwargs):
        real_attempt(streamer, success, **kwargs)
        if success is not True:
            # A refresh finds the next broadcast before the retry.
            streamer.stream.broadcast_id = "b2"
            streamer.stream.payload = _payload("b2")
            journal.record_live(streamer)
            journal.clock_state["mono"] += 1

    monkeypatch.setattr(journal, "watch_attempt", attempt)

    def configure(streamer):
        streamer.stream.payload = _payload("b1")
        streamer.is_online = True

    effects = [
        requests.exceptions.ConnectionError("reset"),
        SimpleNamespace(status_code=204),
    ]
    assert [
        (record["broadcast_id"], record["seconds"])
        for record in _watched(journal, monkeypatch, effects, configure=configure)
    ] == [("b2", 1.0)]


def _payload(broadcast_id):
    return [
        {
            "event": "minute-watched",
            "properties": {"broadcast_id": broadcast_id, "channel": "alice"},
        }
    ]


def test_a_retried_request_credits_only_the_time_since_the_failure(
    journal, monkeypatch
):
    real_attempt = journal.watch_attempt

    def attempt(streamer, success, **kwargs):
        real_attempt(streamer, success, **kwargs)
        if success is not True:
            journal.clock_state["mono"] += 1  # the retry takes a second

    monkeypatch.setattr(journal, "watch_attempt", attempt)
    effects = [
        requests.exceptions.ConnectionError("reset"),
        SimpleNamespace(status_code=204),
    ]

    assert [
        record["seconds"] for record in _watched(journal, monkeypatch, effects)
    ] == [1.0]


@pytest.mark.parametrize(
    "effect",
    [
        SimpleNamespace(status_code=500),
        requests.exceptions.Timeout("slow"),
        requests.exceptions.ConnectionError("down"),
    ],
    ids=["rejected", "timeout", "connection-error"],
)
def test_a_failed_request_credits_nothing(journal, monkeypatch, effect):
    monkeypatch.setattr(Twitch, "_Twitch__check_connection_handler", lambda *a: None)
    assert _watched(journal, monkeypatch, effect) == []


def test_a_request_skipped_for_a_missing_spade_url_credits_nothing(
    journal, monkeypatch
):
    def no_spade(streamer):
        streamer.stream.spade_url = None

    monkeypatch.setattr(
        "TwitchChannelPointsMiner.classes.Twitch.HttpSession.post",
        lambda *a, **k: pytest.fail("posted without a spade URL"),
    )
    streamer = _watch_streamer("alice", explicitly_configured=True)
    no_spade(streamer)
    streamer.stream.broadcast_id = "b1"
    _run_one_watch_iteration(monkeypatch, [streamer], streams_watched=1)
    assert written(journal, "watch") == []
    # The skip moved the boundary: a later success counts only from here.
    journal.clock_state["mono"] += 5
    journal.watch_attempt(streamer, True)
    assert [w["seconds"] for w in written(journal, "watch")] == [5.0]


def test_a_skip_moves_the_boundary_before_the_slot_sleeps(journal, monkeypatch):
    monkeypatch.setattr(
        "TwitchChannelPointsMiner.classes.Twitch.HttpSession.post",
        lambda *a, **k: pytest.fail("posted without a spade URL"),
    )
    streamer = _watch_streamer("alice", explicitly_configured=True)
    streamer.stream.spade_url = None
    streamer.stream.broadcast_id = "b1"

    sleeps = []

    def slot_sleep(twitch, *_args, **_kwargs):
        if not sleeps:  # the skipped slot's wait
            journal.clock_state["mono"] += 20
        sleeps.append(1)
        twitch.running = False

    _run_one_watch_iteration(
        monkeypatch, [streamer], streams_watched=1, on_sleep=slot_sleep
    )
    # The next request succeeds right after the slot's 20 seconds.
    journal.watch_attempt(streamer, True)
    assert [w["seconds"] for w in written(journal, "watch")] == [20.0]


def test_a_drops_only_channel_is_never_credited(journal, monkeypatch):
    farmed = _watch_streamer("farmed", from_category=True, drops_eligible=True)
    assert (
        _watched(journal, monkeypatch, SimpleNamespace(status_code=204), farmed) == []
    )


# -- PubSub --------------------------------------------------------------------
def _on_message(streamer, topic, message_type, data, channel_id="1001"):
    WebSocketsPool.on_message(
        _websocket([streamer]),
        _pubsub(topic, channel_id, {"type": message_type, "data": data}),
    )


@pytest.fixture
def pubsub_streamer(monkeypatch):
    monkeypatch.setattr(
        "TwitchChannelPointsMiner.classes.WebSocketsPool.Settings.enable_analytics",
        False,
    )
    streamer = FakeStreamer("alice", "1001")
    streamer.explicitly_configured = True
    streamer.stream = SimpleNamespace(broadcast_id="b1", game={}, title=None)
    return streamer


def test_a_drop_progress_message_names_the_channel_credited(journal, pubsub_streamer):
    # user-drop-events are the account's: the channel is in the data.
    for channel_id, drop_id in (("1001", "drop-1"), ("9999", "drop-2")):
        WebSocketsPool.on_message(
            _websocket([pubsub_streamer]),
            _pubsub(
                "user-drop-events",
                "42",
                {
                    "type": "drop-progress",
                    "data": {
                        "drop_id": drop_id,
                        "channel_id": channel_id,
                        "current_progress_min": 5,
                        "required_progress_min": 60,
                    },
                },
            ),
        )
    assert journal.drop_channel("drop-1") == "alice"
    # A channel the miner doesn't have.
    assert journal.drop_channel("drop-2") is None


def test_drop_progress_for_two_drops_at_one_time_names_both_channels(
    journal, pubsub_streamer
):
    # The coarse duplicate filter drops a second message of the same type,
    # topic and time, even for another drop.
    ws = _websocket([pubsub_streamer])
    for drop_id in ("drop-1", "drop-2"):
        WebSocketsPool.on_message(
            ws,
            _pubsub(
                "user-drop-events",
                "42",
                {
                    "type": "drop-progress",
                    "data": {
                        "drop_id": drop_id,
                        "channel_id": "1001",
                        "current_progress_min": 5,
                        "required_progress_min": 60,
                    },
                },
            ),
        )
    assert journal.drop_channel("drop-1") == "alice"
    assert journal.drop_channel("drop-2") == "alice"


def test_points_earned_is_journalled(journal, pubsub_streamer):
    _on_message(
        pubsub_streamer, "community-points-user-v1", "points-earned", POINTS_EARNED
    )
    (record,) = written(journal, "points")
    assert (record["streamer"], record["delta"], record["broadcast_id"]) == (
        "alice",
        10,
        "b1",
    )


def test_points_spent_is_journalled_without_an_amount(journal, pubsub_streamer):
    _on_message(
        pubsub_streamer, "community-points-user-v1", "points-spent", POINTS_SPENT
    )
    (record,) = written(journal, "points")
    assert record["delta"] is None


@pytest.mark.parametrize("spent_first", [True, False])
def test_a_redemption_is_one_spend_whatever_the_order(
    journal, pubsub_streamer, spent_first
):
    messages = [("points-spent", POINTS_SPENT), ("reward-redeemed", REWARD_REDEEMED)]
    if not spent_first:
        messages.reverse()
    for message_type, data in messages + [("reward-redeemed", REWARD_REDEEMED)]:
        # The redemption's topic is the user's own; its channel is inside.
        _on_message(
            pubsub_streamer,
            "community-points-user-v1",
            message_type,
            data,
            channel_id="2002",
        )
    (spend,) = written(journal, "spend")
    assert (spend["streamer"], spend["amount"]) == ("alice", 1000)


def test_points_earned_out_of_order_give_the_same_total(journal, pubsub_streamer):
    later = json.loads(json.dumps(POINTS_EARNED))
    later["timestamp"] = "2026-11-23T07:06:25Z"
    later["point_gain"]["reason_code"] = "CLAIM"
    later["point_gain"]["total_points"] = 50
    later["balance"]["balance"] = 1020
    for data in (later, POINTS_EARNED):
        _on_message(pubsub_streamer, "community-points-user-v1", "points-earned", data)
    assert sorted(r["delta"] for r in written(journal, "points")) == [10, 50]


def test_distinct_messages_sharing_a_timestamp_are_all_journalled(
    journal, pubsub_streamer
):
    """The pool's filter drops a message with the previous one's timestamp
    and type; History has its own duplicate check."""

    second = json.loads(json.dumps(POINTS_EARNED))
    second["point_gain"]["reason_code"] = "CLAIM"
    second["point_gain"]["total_points"] = 50
    second["balance"]["balance"] = 1010
    other = json.loads(json.dumps(REWARD_REDEEMED))
    other["redemption"]["id"] = "4629386b-0000-0000-0000-000000000002"
    websocket = _websocket([pubsub_streamer])
    for message_type, data, channel_id in (
        ("points-earned", POINTS_EARNED, "1001"),
        ("points-earned", second, "1001"),
        ("reward-redeemed", REWARD_REDEEMED, "2002"),
        ("reward-redeemed", other, "2002"),
    ):
        WebSocketsPool.on_message(
            websocket,
            _pubsub(
                "community-points-user-v1",
                channel_id,
                {"type": message_type, "data": data},
            ),
        )
    assert sorted(r["delta"] for r in written(journal, "points")) == [10, 50]
    assert len(written(journal, "spend")) == 2


# -- spends the miner makes ---------------------------------------------------------
def _prediction_twitch(response=None, error=None):
    twitch = Twitch.__new__(Twitch)
    gql = MagicMock()
    if error is not None:
        gql.make_prediction.side_effect = error
    else:
        gql.make_prediction.return_value = response
    twitch.gql = gql
    streamer = SimpleNamespace(
        username="alice",
        channel_points=1000,
        explicitly_configured=True,
        from_followers=False,
        from_category=False,
        from_badge_campaign=False,
        from_wildcard_category=False,
    )
    event = SimpleNamespace(
        streamer=streamer,
        status="ACTIVE",
        event_id="event-1",
        title="Who wins?",
        bet=SimpleNamespace(
            calculate=lambda balance: {"amount": 250, "id": "outcome", "choice": "A"},
            skip=lambda: (False, 0),
            get_outcome=lambda choice: "A",
            settings=SimpleNamespace(filter_condition=None),
        ),
    )
    return twitch, event


def _pool_with_bets(make_predictions):
    pool = WebSocketsPool(SimpleNamespace(make_predictions=make_predictions), [], {})
    return pool, SimpleNamespace(parent_pool=pool, twitch=pool.twitch)


def test_stopping_cancels_bets_still_waiting():
    placed = []
    pool, websocket = _pool_with_bets(placed.append)
    assert WebSocketsPool._schedule_prediction(websocket, 0.5, "event")
    pool.end()
    for timer in list(pool.prediction_timers):
        timer.join(timeout=5)
    assert placed == []
    # Nothing new is scheduled once the pool has ended.
    assert WebSocketsPool._schedule_prediction(websocket, 0, "later") is False


def _blocking(response):
    started, release = threading.Event(), threading.Event()

    def request(*_args):
        started.set()
        assert release.wait(timeout=5)
        return response

    return started, release, request


@pytest.mark.parametrize("spend", ["bet", "goal"])
def test_stopping_waits_for_a_spend_being_made(journal, spend):
    """Whichever thread makes it (a bet timer, the main loop, the config
    watcher, a socket handler), a spend Twitch accepts while the miner is
    stopping is recorded before run_end."""

    started, release, request = _blocking(SimpleNamespace(error=None))
    if spend == "bet":
        twitch, event = _prediction_twitch()
        twitch.gql.make_prediction.side_effect = request
        work = threading.Thread(target=twitch.make_predictions, args=(event,))
    else:
        twitch = Twitch.__new__(Twitch)
        twitch.gql = MagicMock()
        twitch.gql.contribute_to_community_goal.side_effect = request
        streamer = Streamer("alice", explicitly_configured=True)
        streamer.channel_points = 1000
        work = threading.Thread(
            target=twitch.contribute_to_community_goal,
            args=(streamer, "goal-1", "Goal", 100),
        )
    work.start()
    assert started.wait(timeout=5)
    stopper = threading.Thread(target=journal.stop)
    stopper.start()
    stopper.join(timeout=0.2)
    assert stopper.is_alive()  # waiting for the spend
    release.set()
    work.join(timeout=5)
    stopper.join(timeout=5)
    assert written(journal)[-2:] == ["spend", "run_end"]


def test_stopping_gives_up_on_a_spend_after_the_limit(journal, monkeypatch):
    monkeypatch.setattr(journal_module, "STOP_SPEND_SECONDS", 0.05)
    started, release, request = _blocking(SimpleNamespace(error=None))
    twitch, event = _prediction_twitch()
    twitch.gql.make_prediction.side_effect = request
    work = threading.Thread(target=twitch.make_predictions, args=(event,))
    work.start()
    assert started.wait(timeout=5)
    journal.stop()
    release.set()
    work.join(timeout=5)
    assert written(journal)[-1] == "run_end"
    assert "spend" not in written(journal)


@pytest.mark.parametrize("spend", ["bet", "goal"])
def test_a_stopped_miner_makes_no_spend(journal, spend):
    """A bet timer already running when the pool cancelled the others, or a
    goal check, reaches the request only after the journal stopped."""

    journal.stop()
    if spend == "bet":
        twitch, event = _prediction_twitch(response=SimpleNamespace(error=None))
        twitch.make_predictions(event)
        twitch.gql.make_prediction.assert_not_called()
    else:
        twitch = Twitch.__new__(Twitch)
        twitch.gql = MagicMock()
        streamer = Streamer("alice", explicitly_configured=True)
        twitch.contribute_to_community_goal(streamer, "goal-1", "Goal", 100)
        twitch.gql.contribute_to_community_goal.assert_not_called()


def test_spends_go_ahead_without_history():
    twitch, event = _prediction_twitch(response=SimpleNamespace(error=None))
    twitch.make_predictions(event)  # the journal never started
    twitch.gql.make_prediction.assert_called_once()


def test_a_new_session_allows_spends_again(journal):
    import TwitchChannelPointsMiner.history as history

    journal.stop()
    history.start_journal()  # analytics off here: no run, but spends allowed
    twitch, event = _prediction_twitch(response=SimpleNamespace(error=None))
    twitch.make_predictions(event)
    twitch.gql.make_prediction.assert_called_once()


def test_a_spend_on_the_stopping_thread_is_not_waited_for(journal, monkeypatch):
    """Ctrl+C runs stop() on the thread it interrupted, maybe mid-spend."""

    import time

    monkeypatch.setattr(journal_module, "STOP_SPEND_SECONDS", 5)
    began = time.monotonic()
    with journal.spend_in_progress():
        journal.stop()
    assert time.monotonic() - began < 2
    assert written(journal)[-1] == "run_end"


def test_an_accepted_bet_is_a_spend_of_the_amount_sent(journal):
    twitch, event = _prediction_twitch(SimpleNamespace(error=None))
    twitch.make_predictions(event)
    (spend,) = written(journal, "spend")
    assert (spend["amount"], spend["spend_key"], spend["source"]) == (
        250,
        "prediction:event-1",
        "prediction",
    )


def test_a_rejected_or_skipped_bet_is_no_spend(journal):
    twitch, event = _prediction_twitch(
        SimpleNamespace(error=SimpleNamespace(code="NOT_ENOUGH_POINTS"))
    )
    twitch.make_predictions(event)
    twitch, event = _prediction_twitch(SimpleNamespace(error=None))
    event.bet.skip = lambda: (True, 0)
    twitch.make_predictions(event)
    assert written(journal, "spend") == []


def test_a_community_goal_contribution_is_a_spend(journal):
    twitch = Twitch.__new__(Twitch)
    twitch.gql = MagicMock()
    twitch.gql.contribute_to_community_goal.side_effect = [
        SimpleNamespace(error=None),
        SimpleNamespace(error="NOT_IN_STOCK"),
        SimpleNamespace(error=None),
    ]
    streamer = Streamer("alice", explicitly_configured=True)
    streamer.channel_points = 1000
    for _ in range(3):
        twitch.contribute_to_community_goal(streamer, "goal-1", "Goal", 100)
    spends = written(journal, "spend")
    assert [spend["amount"] for spend in spends] == [100, 100]
    assert spends[0]["spend_key"] != spends[1]["spend_key"]


# -- lifecycle -------------------------------------------------------------------
def _online_streamer(name="alice", **flags):
    streamer = Streamer(name, settings=streamer_settings(), **flags)
    streamer.stream.broadcast_id = "b1"
    streamer.stream.title = "Title"
    streamer.stream.game = {"displayName": "Game"}
    return streamer


def test_going_online_and_offline_is_journalled(journal):
    streamer = _online_streamer(explicitly_configured=True)
    streamer.set_online()
    streamer.set_online()
    streamer.set_offline()
    streamer.set_online()
    assert written(journal) == ["live", "offline", "live"]


def test_a_stream_update_with_a_new_title_is_journalled(journal):
    streamer = _online_streamer(explicitly_configured=True)
    streamer.set_online()
    twitch = Twitch.__new__(Twitch)
    twitch.twitch_login = SimpleNamespace(get_user_id=lambda: "2002")
    for title in ("Title", "Another title"):
        twitch._apply_stream_update(
            streamer,
            (
                0.0,
                {
                    "stream": {"id": "b1", "tags": [], "viewersCount": 1},
                    "broadcastSettings": {
                        "title": title,
                        "game": {"id": "1", "name": "game", "displayName": "Game"},
                    },
                },
            ),
        )
    assert [r["title"] for r in written(journal, "live")] == ["Title", "Another title"]


def test_online_and_offline_on_two_threads_keep_their_order(journal, monkeypatch):
    """The transition and its record are one step under the lifecycle lock."""

    streamer = _online_streamer(explicitly_configured=True)
    entered = threading.Event()
    release = threading.Event()
    real_record = journal.record_live

    def slow_record(target):
        entered.set()
        release.wait(timeout=5)
        real_record(target)

    monkeypatch.setattr(journal, "record_live", slow_record)
    online = threading.Thread(target=streamer.set_online)
    online.start()
    assert entered.wait(timeout=5)
    offline = threading.Thread(target=streamer.set_offline)
    offline.start()
    offline.join(timeout=0.2)
    assert offline.is_alive()
    release.set()
    online.join(timeout=5)
    offline.join(timeout=5)
    assert written(journal) == ["live", "offline"]


def _reload_miner(streamers):
    websocket_pool = SimpleNamespace(removed=[])
    websocket_pool.remove_streamer_topics = websocket_pool.removed.append
    return SimpleNamespace(
        running=True,
        ws_pool=websocket_pool,
        config_reload_lock=threading.Lock(),
        streamers=list(streamers),
        original_streamers=[0] * len(streamers),
        _record_daily_report_retirements=lambda retained: None,
    )


def test_removing_a_streamer_writes_untracked(journal):
    explicit = _online_streamer("explicit", explicitly_configured=True)
    explicit.set_online()
    TwitchChannelPointsMiner.remove_streamers(_reload_miner([explicit]), ["explicit"])
    assert written(journal) == ["live", "untracked"]


def test_a_removed_streamer_is_not_reopened_by_a_refresh_in_flight(journal):
    explicit = _online_streamer("explicit", explicitly_configured=True)
    explicit.set_online()
    TwitchChannelPointsMiner.remove_streamers(_reload_miner([explicit]), ["explicit"])
    # The watcher's snapshot still holds the removed object.
    explicit.stream.title = "Another title"
    journal_module.JOURNAL.record_live(explicit)
    explicit.set_offline()
    assert written(journal) == ["live", "untracked"]


def test_a_removed_streamer_object_added_back_is_recorded_again(journal, monkeypatch):
    import importlib

    from TwitchChannelPointsMiner.classes.Chat import ChatPresence

    miner_module = importlib.import_module(
        "TwitchChannelPointsMiner.TwitchChannelPointsMiner"
    )

    monkeypatch.setattr(
        miner_module, "set_default_settings", lambda settings, _defaults: settings
    )
    alice = _online_streamer("alice", explicitly_configured=True)
    monkeypatch.setattr(
        miner_module.Settings, "streamer_settings", alice.settings, raising=False
    )
    alice.set_online()
    TwitchChannelPointsMiner.remove_streamers(_reload_miner([alice]), ["alice"])
    miner = SimpleNamespace(
        streamers=[],
        original_streamers=[],
        twitch=MagicMock(),
        ws_pool=MagicMock(),
        username="viewer",
        _record_daily_report_streamers=lambda streamers: None,
        _TwitchChannelPointsMiner__subscribe_user_drop_events=lambda: None,
    )
    alice.settings.chat = ChatPresence.NEVER
    # Still online from before; the (cached) online check changes nothing.
    TwitchChannelPointsMiner._add_streamers(miner, [alice])
    assert written(journal) == ["live", "untracked", "live"]


def test_demoting_a_channel_kept_for_drops_writes_untracked(journal):
    kept = _online_streamer("kept", from_category=True, explicitly_configured=True)
    kept.set_online()
    TwitchChannelPointsMiner.remove_streamers(_reload_miner([kept]), ["kept"])
    assert written(journal) == ["live", "untracked"]
    # Now a Drops-only channel: nothing more is recorded.
    kept.set_offline()
    assert written(journal) == ["live", "untracked"]


def test_promoting_an_online_drops_channel_writes_live(journal):
    farmed = _online_streamer("farmed", from_category=True)
    farmed.set_online()
    assert written(journal) == []
    miner = SimpleNamespace(streamers=[farmed], original_streamers=[0])
    TwitchChannelPointsMiner._add_streamers(miner, ["farmed"])
    assert written(journal) == ["live"]


class _AfterFilter(Exception):
    """Raised once startup has dropped the streamers it could not load."""


class _StopOnAssign(list):
    def __setitem__(self, index, value):
        raise _AfterFilter()


def test_a_streamer_rejected_at_startup_is_closed(journal, monkeypatch):
    """Startup checks a streamer online (writing ``live``) and then drops it:
    its stream is closed, not left open for the importer."""

    from test_wildcard_categories import FakeTwitch, _bare_miner

    import TwitchChannelPointsMiner.history as history

    monkeypatch.setattr(history, "start_journal", lambda: None)
    twitch = FakeTwitch(
        eligible_categories=["gamex"],
        wildcard_categories=[],
    )

    def initialize(streamers):
        for streamer in streamers:
            streamer.stream.broadcast_id = "b1"
            streamer.set_online()
        return {streamer.username for streamer in streamers}

    twitch.login = lambda: None
    twitch.initialize_streamers_context = initialize
    miner = _bare_miner(twitch)
    miner.original_streamers = _StopOnAssign()
    miner.running = False
    miner.session_id = "test-session"
    miner.claim_drops_startup = False
    miner.watch_streak_cache = None
    with pytest.raises(_AfterFilter):
        miner.run(streamers=["alice"], drop_badge_catalog=False)
    assert miner.streamers == []
    assert written(journal) == ["live", "untracked"]


def test_a_connection_failure_moves_the_boundary_before_the_recovery_wait(
    journal, monkeypatch
):
    def recovery(twitch, *_args):
        journal.clock_state["mono"] += 60  # waiting for the connection

    monkeypatch.setattr(Twitch, "_Twitch__check_connection_handler", recovery)
    streamer = _watch_streamer("alice", explicitly_configured=True)
    effects = [requests.exceptions.ConnectionError("down")] * 2
    assert _watched(journal, monkeypatch, effects, streamer=streamer) == []
    # The next request succeeds right after the wait.
    journal.watch_attempt(streamer, True)
    assert [w["seconds"] for w in written(journal, "watch")] == [60.0]


def _no_decision(result):
    raise KeyError("amount")


def _prediction(decision):
    return SimpleNamespace(
        title="Will it work?",
        bet_confirmed=False,
        confirmed_points=None,
        result={"string": "", "type": None, "gained": 0},
        # The miner's own bookkeeping needs its decision ({} if it made
        # none); History must not depend on it.
        parse_result=_no_decision,
        bet=SimpleNamespace(
            decision=decision,
            outcomes=[
                {"id": "outcome-yes", "title": "Yes", "color": "BLUE"},
                {"id": "outcome-no", "title": "No", "color": "PINK"},
            ],
        ),
    )


def _prediction_made(streamer, event, outcome_id, points=None):
    websocket = _websocket([streamer])
    websocket.events_predictions["event-1"] = event
    WebSocketsPool.on_message(
        websocket,
        _pubsub(
            "predictions-user-v1",
            "1001",
            {
                "type": "prediction-made",
                "data": {
                    "timestamp": "2026-11-25T12:00:00Z",
                    "prediction": {
                        "event_id": "event-1",
                        "channel_id": "1001",
                        "outcome_id": outcome_id,
                        **({} if points is None else {"points": points}),
                    },
                },
            },
        ),
    )
    return event


@pytest.mark.parametrize(
    "decision", [{}, {"choice": 0}], ids=["before-the-timer", "other-outcome"]
)
def test_a_bet_placed_by_hand_is_labelled_with_its_own_outcome(
    journal, pubsub_streamer, decision
):
    event = _prediction_made(pubsub_streamer, _prediction(decision), "outcome-no")
    assert event.bet_confirmed is True
    (record,) = written(journal, "event")
    assert (record["type"], record["label"]) == (
        "prediction_made",
        "Decision: No - Will it work?",
    )


def test_a_bet_on_an_unknown_outcome_is_labelled_with_the_title(
    journal, pubsub_streamer
):
    _prediction_made(pubsub_streamer, _prediction({}), "outcome-gone")
    (record,) = written(journal, "event")
    assert record["label"] == "Will it work?"


def _prediction_result(streamer, event, result, points=None):
    websocket = _websocket([streamer])
    websocket.events_predictions["event-1"] = event
    prediction = {"event_id": "event-1", "channel_id": "1001", "result": result}
    if points is not None:
        prediction["points"] = points
    WebSocketsPool.on_message(
        websocket,
        _pubsub(
            "predictions-user-v1",
            "1001",
            {
                "type": "prediction-result",
                "data": {"timestamp": "2026-11-25T12:05:00Z", "prediction": prediction},
            },
        ),
    )


def test_a_hand_placed_bet_the_miner_skipped_gains_what_was_won_less_the_wager(
    journal, pubsub_streamer
):
    """The miner calculated 250 but skipped; the user wagered 50 by hand."""

    event = _prediction({"choice": 0, "amount": 250})
    _prediction_made(pubsub_streamer, event, "outcome-no", points=50)
    _prediction_result(pubsub_streamer, event, {"type": "WIN", "points_won": 150})
    (record,) = written(journal, "event")[1:]
    assert (record["type"], record["points_delta"]) == ("win", 100)


def test_a_result_before_the_miner_decided_is_journalled(journal, pubsub_streamer):
    event = _prediction({})
    _prediction_made(pubsub_streamer, event, "outcome-no", points=50)
    _prediction_result(pubsub_streamer, event, {"type": "LOSE", "points_won": None})
    (record,) = written(journal, "event")[1:]
    assert (record["type"], record["points_delta"]) == ("lose", -50)


def test_a_result_with_an_unknown_wager_has_no_delta(journal, pubsub_streamer):
    event = _prediction({})
    _prediction_made(pubsub_streamer, event, "outcome-no")
    _prediction_result(pubsub_streamer, event, {"type": "WIN", "points_won": 150})
    (record,) = written(journal, "event")[1:]
    assert (record["type"], record["points_delta"]) == ("win", None)


def test_a_stream_update_cannot_come_between_the_body_and_its_token(
    journal, monkeypatch
):
    """The watcher reads the payload and History's token as one step: a
    broadcast change on another thread waits for both."""

    from TwitchChannelPointsMiner.classes import Twitch as twitch_module

    streamer = _watch_streamer("alice", explicitly_configured=True)
    streamer.stream.payload = _payload("b1")
    streamer.stream.broadcast_id = "b1"
    real_request = twitch_module._minute_watched_request
    blocked = []

    def change_broadcast():
        with journal_module.lifecycle_lock("alice"):
            streamer.stream.payload = _payload("b2")
            streamer.stream.broadcast_id = "b2"
            journal.record_live(streamer)

    def request(stream):
        body = real_request(stream)
        if not blocked:
            other = threading.Thread(target=change_broadcast, daemon=True)
            other.start()
            other.join(0.2)
            blocked.append(other.is_alive())
            blocked.append(other)
        return body

    real_attempt = journal.watch_attempt

    def attempt(*args, **kwargs):
        # The b1 reply arrives after the switch.  The watcher posts once it
        # leaves the lock, so without this wait its reply could be handled
        # before the other thread takes the lock.
        if len(blocked) > 1:
            blocked[1].join(5)
        return real_attempt(*args, **kwargs)

    monkeypatch.setattr(twitch_module, "_minute_watched_request", request)
    monkeypatch.setattr(journal, "watch_attempt", attempt)
    _watched(journal, monkeypatch, SimpleNamespace(status_code=204), streamer)
    blocked[1].join(5)
    assert blocked[0] is True
    assert not blocked[1].is_alive()
    # The b1 reply came after the switch (an ended broadcast: nothing);
    # History is on b2, and b2's next accepted request is credited.
    journal.clock_state["mono"] += 20
    journal.watch_attempt(
        streamer, True, broadcast_id="b2", token=journal.watch_token(streamer)
    )
    assert [
        (record["broadcast_id"], record["seconds"])
        for record in written(journal, "watch")
    ] == [("b2", 20.0)]


def test_a_missed_broadcast_change_is_one_step_for_the_watcher(journal, monkeypatch):
    """A refresh shows a new broadcast and runs the offline/online the miner
    missed.  The watcher reads the payload and its token only before or
    after all of it, so a request for the new broadcast is credited."""

    from TwitchChannelPointsMiner.classes import Twitch as twitch_module
    from TwitchChannelPointsMiner.classes.Chat import ChatPresence

    streamer = _online_streamer("alice", explicitly_configured=True)
    streamer.settings.chat = ChatPresence.NEVER
    streamer.set_online()
    journal.watch_selection({"alice": (streamer, "points")})
    twitch = Twitch.__new__(Twitch)
    twitch.twitch_login = SimpleNamespace(get_user_id=lambda: "2002")
    monkeypatch.setattr(
        Twitch, "_Twitch__streamer_drops_description", lambda self, s: None
    )
    captured = []
    watcher = []

    def capture():
        with journal_module.lifecycle_lock("alice"):
            _body, sent_for = twitch_module._minute_watched_request(streamer.stream)
            captured.append((sent_for, journal.watch_token(streamer)))

    real_set_offline = Streamer.set_offline

    def set_offline(self, *args, **kwargs):
        if not watcher:
            # The watcher's next request, between the update and the
            # offline/online.
            thread = threading.Thread(target=capture, daemon=True)
            thread.start()
            thread.join(0.2)
            watcher.append(thread)
        return real_set_offline(self, *args, **kwargs)

    monkeypatch.setattr(Streamer, "set_offline", set_offline)
    twitch._apply_stream_update_and_transition(
        streamer,
        (
            0.0,
            {
                "stream": {"id": "b2", "tags": [], "viewersCount": 1},
                "broadcastSettings": {
                    "title": "Title",
                    "game": {"id": "1", "name": "game", "displayName": "Game"},
                },
            },
        ),
    )
    watcher[0].join(5)
    assert not watcher[0].is_alive()
    ((sent_for, token),) = captured
    journal.clock_state["mono"] += 20
    journal.watch_attempt(streamer, True, broadcast_id=sent_for, token=token)
    assert [
        (record["broadcast_id"], record["seconds"])
        for record in written(journal, "watch")
    ] == [("b2", 20.0)]
