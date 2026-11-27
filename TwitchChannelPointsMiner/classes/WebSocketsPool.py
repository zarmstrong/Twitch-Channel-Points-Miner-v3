import functools
import json
import logging
import random
import time
from datetime import timezone

# import os
from threading import Lock, RLock, Thread, Timer

from dateutil import parser

from TwitchChannelPointsMiner.classes.CopyOnWriteList import CopyOnWriteList
from TwitchChannelPointsMiner.classes.entities.CommunityGoal import CommunityGoal
from TwitchChannelPointsMiner.classes.entities.EventPrediction import EventPrediction
from TwitchChannelPointsMiner.classes.entities.Message import Message
from TwitchChannelPointsMiner.classes.entities.Raid import Raid
from TwitchChannelPointsMiner.classes.entities.Streamer import (
    bump_streamer_state,
    is_drop_discovered,
    run_under_state_lock,
)
from TwitchChannelPointsMiner.classes.Settings import Events, Settings
from TwitchChannelPointsMiner.classes.TwitchWebSocket import TwitchWebSocket
from TwitchChannelPointsMiner.constants import WEBSOCKET
from TwitchChannelPointsMiner.history import journal as history_journal
from TwitchChannelPointsMiner.utils import get_streamer, internet_connection_available

# from pathlib import Path


logger = logging.getLogger(__name__)


def _sent_at(message):
    """Twitch's send time of a PubSub message in epoch seconds, or None."""
    try:
        sent = parser.parse(message.timestamp)
    except (TypeError, ValueError, OverflowError):
        return None
    if sent.tzinfo is None:
        # Twitch's timestamps are UTC; without an offset, timestamp() would
        # read it as the host's local time.
        sent = sent.replace(tzinfo=timezone.utc)
    try:
        return sent.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _created_at(event_prediction):
    """A prediction's creation time in epoch seconds, or None."""

    created = getattr(event_prediction, "created_at", None)
    try:
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return created.timestamp()
    except (AttributeError, TypeError, ValueError, OverflowError, OSError):
        return None


def _apply_stream_down(message, streamer, received_at):
    """Handle a stream-down; called under the streamer's state lock, maybe
    later than it arrived (received_at, local time)."""
    if streamer.is_online is not True:
        return
    seen_live_at = getattr(getattr(streamer, "stream", None), "live_seen_at", 0) or 0
    if seen_live_at > received_at:
        # A refresh sent after this stream-down arrived found the stream
        # live: newer than the stream-down, so the stream stays online.
        logger.debug(
            f"Ignoring a stream-down for {streamer.username}: a later "
            "refresh found it live"
        )
        return
    if _stream_down_predates_online(message, streamer):
        # Ask Twitch on the next online check instead of ending a broadcast
        # that may still be live.
        request_refresh = getattr(streamer.stream, "request_refresh", None)
        if callable(request_refresh):
            request_refresh()
        logger.debug(
            f"Rechecking a stream-down for {streamer.username} sent before "
            "it was last marked online"
        )
        return
    streamer.set_offline()


def _check_online_in_background(twitch, streamer):
    """Run a websocket-triggered online check on its own thread.

    The check makes Twitch requests; on the socket's thread it would hold up
    every later message on that socket (stream-downs, awards, predictions,
    pongs). It doesn't wait for a refresh already running on another thread.
    """

    def check():
        try:
            twitch.check_streamer_online(streamer, blocking=False)
        except Exception:
            logger.error(
                f"Exception raised while checking whether {streamer.username} "
                "is online",
                exc_info=True,
            )

    Thread(target=check, name=f"online-check-{streamer.username}", daemon=True).start()


def _stream_down_predates_online(message, streamer):
    """Whether Twitch sent this stream-down before the miner last marked the
    streamer online (see Streamer.sent_before_online()).

    Such a stream-down can be about the earlier broadcast: delayed in
    delivery, or sent while the refresh that found the next broadcast was
    in flight. It can also be the current broadcast's, if that ended while
    the reply that showed it live was in flight. Only Twitch can tell.
    """
    sent_before_online = getattr(streamer, "sent_before_online", None)
    return (
        callable(sent_before_online) and sent_before_online(_sent_at(message)) is True
    )


class WebSocketsPool:
    __slots__ = [
        "ws",
        "twitch",
        "streamers",
        "events_predictions",
        "topic_lock",
        "prediction_timers",
        "ended",
    ]

    def __init__(self, twitch, streamers, events_predictions):
        # Read without topic_lock by the main loop's checks; see CopyOnWriteList.
        self.ws = CopyOnWriteList()
        self.twitch = twitch
        self.streamers = streamers
        self.events_predictions = events_predictions
        self.topic_lock = RLock()
        # Bets scheduled to be placed, and whether end() ran; under
        # topic_lock.
        self.prediction_timers = set()
        self.ended = False

    """
    API Limits
    - Clients can listen to up to 50 topics per connection. Trying to listen to more topics will result in an error message.
    - We recommend that a single client IP address establishes no more than 10 simultaneous connections.
    The two limits above are likely to be relaxed for approved third-party applications, as we start to better understand third-party requirements.
    """

    def submit(self, topic):
        with self.topic_lock:
            index = None
            available_index = None
            for current_index, websocket in enumerate(self.ws):
                if topic in websocket.topics:
                    index = current_index
                    break
                if available_index is None and len(websocket.topics) < 50:
                    available_index = current_index
            if index is None:
                index = available_index
            if index is None:
                self.ws.append(self.__new(len(self.ws)))
                self.__start(-1)
                index = len(self.ws) - 1
            websocket, should_listen = self.__register(index, topic)

        if should_listen:
            self.__listen(websocket, topic)

    def remove_streamer_topics(self, streamer):
        """Unsubscribe and forget every PubSub topic owned by a streamer."""
        with self.topic_lock:
            removals = []
            for websocket in self.ws:
                removed = [
                    topic for topic in websocket.topics if topic.streamer is streamer
                ]
                websocket.topics[:] = [
                    topic
                    for topic in websocket.topics
                    if topic.streamer is not streamer
                ]
                websocket.pending_topics[:] = [
                    topic
                    for topic in websocket.pending_topics
                    if topic.streamer is not streamer
                ]
                removals.append((websocket, removed))
            retired = []
            while self.ws and not self.ws[-1].topics:
                websocket = self.ws.pop()
                websocket.forced_close = True
                retired.append(websocket)
        for websocket, removed in removals:
            if websocket not in retired and websocket.is_opened and removed:
                auth_token = self.twitch.twitch_login.get_auth_token()
                for topic in removed:
                    websocket.unlisten(topic, auth_token)
        for websocket in retired:
            websocket.close()

    def __submit(self, index, topic, replay=False, target=None):
        with self.topic_lock:
            # A replay targets one specific websocket, which may have been
            # retired from the pool since the replay started.
            if target is not None and not self.__holds(index, target):
                return
            if replay and topic not in self.ws[index].topics:
                return
            websocket, should_listen = self.__register(index, topic)
            if replay and websocket.is_opened:
                should_listen = True

        if should_listen:
            self.__listen(websocket, topic)

    def __register(self, index, topic):
        websocket = self.ws[index]
        # Topic in topics should never happen. Anyway prevent any types of duplicates
        newly_added = topic not in websocket.topics
        if newly_added:
            websocket.topics.append(topic)
        if websocket.is_opened is False:
            if topic not in websocket.pending_topics:
                websocket.pending_topics.append(topic)
            return websocket, False
        return websocket, newly_added

    def __listen(self, websocket, topic, auth_token=None):
        if auth_token is None:
            auth_token = self.twitch.twitch_login.get_auth_token()
        websocket.listen(topic, auth_token)
        with self.topic_lock:
            still_configured = websocket in self.ws and topic in websocket.topics
        if not still_configured:
            websocket.unlisten(topic, auth_token)

    def __new(self, index):
        return TwitchWebSocket(
            index=index,
            parent_pool=self,
            url=WEBSOCKET,
            on_message=WebSocketsPool.on_message,
            on_open=WebSocketsPool.on_open,
            on_error=WebSocketsPool.on_error,
            on_close=WebSocketsPool.on_close,
            # on_close=WebSocketsPool.handle_reconnection, # Do nothing.
        )

    def __holds(self, index, websocket):
        return 0 <= index < len(self.ws) and self.ws[index] is websocket

    def __start(self, index):
        # Resolve the websocket now: the pool can shrink before the new
        # thread gets around to running it.
        websocket = self.ws[index]

        def run_forever(**kwargs):
            if websocket.forced_close is True:
                return
            websocket.run_forever(**kwargs)

        if Settings.disable_ssl_cert_verification is True:
            import ssl

            thread_ws = Thread(
                target=lambda: run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})
            )
            logger.warn("SSL certificate verification is disabled! Be aware!")
        else:
            thread_ws = Thread(target=run_forever)
        thread_ws.daemon = True
        thread_ws.name = f"WebSocket #{websocket.index}"
        thread_ws.start()

    def end(self):
        # Mark under topic_lock so a reconnection in progress either installs
        # its replacement before this snapshot (and it gets closed here) or
        # sees forced_close and gives up. Close outside the lock: close() can
        # call back into on_close.
        with self.topic_lock:
            websockets = list(self.ws)
            for websocket in websockets:
                websocket.forced_close = True
            # A stopped miner places no more bets.
            self.ended = True
            for timer in getattr(self, "prediction_timers", ()):
                timer.cancel()
        for websocket in websockets:
            websocket.close()

    @staticmethod
    def _record_prediction_history(streamer, event_prediction, result_type, points):
        """Count a prediction's result in the streamer's session history.

        community-points-user-v1 credits the payout of a win (as
        PREDICTION) or the points a refund returned (as REFUND); the
        prediction is counted once, with its net change.  A prediction whose
        net change isn't known is left out: the credit is taken back, by the
        amount the result reports or, without one, by the credit itself
        (claim_credit), whenever it arrives."""

        placed = points["placed"]
        won = points["won"]
        if result_type == "REFUND":
            if won is None:
                won = event_prediction.claim_credit("REFUND")
            streamer.update_history("PREDICTION", 0)
            if won is not None:
                streamer.update_history("REFUND", -won, counter=-1)
        elif result_type == "WIN":
            if placed is not None and won is not None:
                streamer.update_history("PREDICTION", won - placed)
                streamer.update_history("PREDICTION", -won, counter=-1)
            elif placed is not None:
                # The payout credit stands for the payout: take the wager off.
                streamer.update_history("PREDICTION", -placed, counter=0)
            else:
                if won is None:
                    won = event_prediction.claim_credit("PREDICTION")
                if won is not None:
                    streamer.update_history("PREDICTION", -won, counter=-1)
        elif placed is not None:
            streamer.update_history("PREDICTION", -placed)

    @staticmethod
    def _note_prediction_credit(ws, streamer, reason_code, earned, credited_at):
        """Keep a PREDICTION or REFUND credit with the prediction it pays
        out, or take it back at once if its result needs that.

        A channel has one prediction at a time, so the credit belongs to the
        streamer's latest prediction created before it was sent
        (``credited_at``, Twitch's time in epoch seconds): a later one was
        created after the credited prediction ended."""

        if reason_code not in ("PREDICTION", "REFUND"):
            return
        events = [
            event
            for event in list(ws.events_predictions.values())
            if event.streamer is streamer
        ]
        created = [(_created_at(event), event) for event in events]
        if credited_at is not None and any(at is not None for at, _ in created):
            earlier = [
                (at, event)
                for at, event in created
                if at is not None and at <= credited_at
            ]
            if not earlier:
                return
            event = max(earlier, key=lambda pair: pair[0])[1]
        elif events:
            event = events[-1]
        else:
            return
        if event.note_credit(reason_code, earned):
            streamer.update_history(reason_code, -earned, counter=-1)

    @staticmethod
    def _report_prediction_result(streamer, event_prediction, result):
        """Log and count a prediction-result's ``result`` for the bet."""

        points = event_prediction.parse_result(result)
        WebSocketsPool._record_prediction_result(streamer, event_prediction, points)

        logger.info(
            (
                f"{event_prediction} - "
                f"{WebSocketsPool._placed_decision(event_prediction)}"
                f"Result: {event_prediction.result['string']}"
            ),
            extra={
                "emoji": ":bar_chart:",
                "event": Events.get(f"BET_{event_prediction.result['type']}"),
            },
        )

        WebSocketsPool._record_prediction_history(
            streamer, event_prediction, event_prediction.result["type"], points
        )

        if event_prediction.result["type"]:
            # Analytics switch
            if Settings.enable_analytics is True:
                streamer.persistent_annotations(
                    event_prediction.result["type"], f"{event_prediction.title}"
                )

    @staticmethod
    def _placed_decision(event_prediction):
        """The "Decision: ..." part of a prediction's result line: the
        outcome the account bet on, or nothing when it isn't known."""

        choice = event_prediction.placed_choice()
        if choice is None:
            return ""
        outcome = event_prediction.bet.outcomes[choice]
        return (
            f"Decision: {choice}: {outcome.get('title')} "
            f"({outcome.get('color')}) - "
        )

    @staticmethod
    def _record_prediction_result(streamer, event_prediction, points):
        """Journal a prediction's result for History with what the account
        gained (``points`` from parse_result): unknown (None) when the
        result line says so."""

        history_journal.JOURNAL.record_event(
            streamer,
            event_prediction.result["type"],
            f"{event_prediction.title}",
            points_delta=points["gained"],
        )

    @staticmethod
    def _prediction_made_label(event_prediction, data):
        """History's label for a prediction the account made, from the
        message itself: the bet may have been placed by hand, before the
        miner decided anything or on another outcome."""

        title = event_prediction.title
        prediction = data.get("prediction") if isinstance(data, dict) else None
        outcome_id = (
            prediction.get("outcome_id") if isinstance(prediction, dict) else None
        )
        outcomes = getattr(event_prediction.bet, "outcomes", None) or ()
        for outcome in outcomes:
            if (
                outcome_id is not None
                and isinstance(outcome, dict)
                and outcome.get("id") == outcome_id
                and outcome.get("title")
            ):
                return f"Decision: {outcome['title']} - {title}"
        return f"{title}"

    @staticmethod
    def _schedule_prediction(ws, delay, event):
        """Place ``event``'s bet after ``delay`` seconds; False once the pool
        has ended."""

        timer = Timer(delay, ws.twitch.make_predictions, (event,))
        timer.daemon = True
        pool = getattr(ws, "parent_pool", None)
        if pool is None:
            timer.start()
            return True
        with pool.topic_lock:
            if getattr(pool, "ended", False):
                return False
            timers = getattr(pool, "prediction_timers", None)
            if timers is not None:
                for started in list(timers):
                    alive = getattr(started, "is_alive", None)
                    if callable(alive) and not alive():
                        timers.discard(started)
                timers.add(timer)
            timer.start()
        return True

    @staticmethod
    def on_open(ws):
        # run_forever() resets keep_running to True, so a close() that landed
        # before it started (end() racing a freshly started socket) is lost.
        # Re-apply it now that the socket is connected and can be closed.
        if ws.forced_close is True:
            ws.close()
            return

        def run():
            ws.is_opened = True
            ws.ping()

            with ws.parent_pool.topic_lock:
                pending_topics = list(ws.pending_topics)
                ws.pending_topics.clear()
            for topic in pending_topics:
                ws.parent_pool.__listen(ws, topic)

            while ws.is_closed is False:
                # Else: the ws is currently in reconnecting phase, you can't do ping or other operation.
                # Probably this ws will be closed very soon with ws.is_closed = True
                if ws.is_reconnecting is False:
                    ws.ping()  # We need ping for keep the connection alive
                    time.sleep(random.uniform(25, 30))

                    if ws.elapsed_last_pong() > 5:
                        logger.info(
                            f"#{ws.index} - The last PONG was received more than 5 minutes ago"
                        )
                        WebSocketsPool.handle_reconnection(ws)

        thread_ws = Thread(target=run)
        thread_ws.daemon = True
        thread_ws.start()

    @staticmethod
    def on_error(ws, error):
        # Connection lost | [WinError 10054] An existing connection was forcibly closed by the remote host
        # Connection already closed | Connection is already closed (raise WebSocketConnectionClosedException)
        logger.error(f"#{ws.index} - WebSocket error: {error}")

    @staticmethod
    def on_close(ws, close_status_code, close_reason):
        logger.info(f"#{ws.index} - WebSocket closed")
        # On close please reconnect automatically
        WebSocketsPool.handle_reconnection(ws)

    @staticmethod
    def handle_reconnection(ws):
        # Reconnect only if ws.is_reconnecting is False to prevent more than 1 ws from
        # being created. on_close (the websocket's own thread) and the ping health
        # check (a separate thread started in on_open) can both call this at once,
        # so the check-and-set must happen under a lock instead of as two statements.
        # Defensive getattr/set: tests exercise this with plain duck-typed
        # stand-ins for TwitchWebSocket that don't carry a reconnect_lock.
        lock = getattr(ws, "reconnect_lock", None)
        if lock is None:
            lock = Lock()
            ws.reconnect_lock = lock
        with lock:
            if ws.is_reconnecting is not False:
                return
            # Close the current WebSocket.
            ws.is_closed = True
            ws.keep_running = False
            # Reconnect only if ws.forced_close is False (replace the keep_running)

            # Set the current socket as reconnecting status
            # So the external ping check will be locked
            ws.is_reconnecting = True

        # keep_running = False only stops run_forever() after its current
        # read, which never returns on a half-open connection, leaking the
        # thread. Close the socket too. Outside the lock: close() calls back
        # into on_close, which lands here and returns at is_reconnecting.
        # Abort first: close() sends a close frame and waits on the reader's
        # lock, so on a half-open connection it could block this reconnect
        # forever. Shutting the socket down wakes the blocked reader and
        # sender, and close() then fails fast.
        abort = getattr(getattr(ws, "sock", None), "abort", None)
        if abort is not None:
            try:
                abort()
            except Exception as error:
                logger.debug(
                    f"#{ws.index} - Aborting the old WebSocket failed: {error}"
                )
        close = getattr(ws, "close", None)
        if close is not None:
            try:
                close()
            except Exception as error:
                logger.debug(f"#{ws.index} - Closing the old WebSocket failed: {error}")

        if ws.forced_close is False:
            logger.info(
                f"#{ws.index} - Reconnecting to Twitch PubSub server in ~60 seconds"
            )
            time.sleep(30)

            while internet_connection_available() is False:
                random_sleep = random.randint(1, 3)
                logger.warning(
                    f"#{ws.index} - No internet connection available! Retry after {random_sleep}m"
                )
                time.sleep(random_sleep * 60)

            # Why not create a new ws on the same array index? Let's try.
            self = ws.parent_pool
            with self.topic_lock:
                # The socket may have been retired (popped from the pool by
                # remove_streamer_topics) while this thread was sleeping.
                if ws.forced_close is True or not self.__holds(ws.index, ws):
                    logger.info(
                        f"#{ws.index} - WebSocket was retired, not reconnecting"
                    )
                    return
                topics = list(ws.topics)
                replacement = self.__new(ws.index)
                replacement.topics.extend(topics)
                self.ws[ws.index] = replacement
                self.__start(ws.index)  # Start a new thread.

            time.sleep(30)

            for topic in topics:
                self.__submit(ws.index, topic, replay=True, target=replacement)

    @staticmethod
    def _record_history(ws, message):
        """Journal a points message or redemption for History (which applies
        its own eligibility rule and duplicate check)."""

        if message.topic != "community-points-user-v1":
            return
        try:
            if message.type == "reward-redeemed":
                # A redemption's generic channel_id falls back to the user's
                # own id, so find its channel from the redemption itself.
                redemption = (message.data or {}).get("redemption") or {}
                redeemed_on = get_streamer(ws.streamers, redemption.get("channel_id"))
                if redeemed_on is not None:
                    history_journal.JOURNAL.record_redemption(redeemed_on, message.data)
            elif message.type in ("points-earned", "points-spent"):
                streamer = get_streamer(ws.streamers, message.channel_id)
                if streamer is not None:
                    history_journal.JOURNAL.record_points_message(
                        streamer, message.type, message.data
                    )
        except Exception:
            logger.debug("Unable to journal a points message", exc_info=True)

    @staticmethod
    def on_message(ws, message):
        logger.debug(f"#{ws.index} - Received: {message.strip()}")
        response = json.loads(message)

        if response["type"] == "MESSAGE":
            # We should create a Message class ...
            message = Message(response["data"])

            # History dedupes points messages and redemptions itself, so it
            # sees them before the coarse filter below, which also drops a
            # distinct message that shares a timestamp with the previous one.
            WebSocketsPool._record_history(ws, message)

            # If we have more than one PubSub connection, messages may be duplicated
            # Check the concatenation between message_type.top.channel_id
            if (
                ws.last_message_type_channel is not None
                and ws.last_message_timestamp is not None
                and ws.last_message_timestamp == message.timestamp
                and ws.last_message_type_channel == message.identifier
            ):
                return

            ws.last_message_timestamp = message.timestamp
            ws.last_message_type_channel = message.identifier

            # user-drop-events are per user: channel_id may be a channel we don't
            # track, so handle them before the streamer lookup.
            if message.topic == "user-drop-events":
                if message.type == "drop-claim":
                    ws.twitch.claim_drops_from_event(message.data)
                elif message.type == "drop-progress":
                    logger.debug(f"Drop progress: {message.data}")
                    ws.twitch.record_drop_progress_event(message.data)
                return

            # History recorded the redemption above; nothing else uses it.
            if (
                message.topic == "community-points-user-v1"
                and message.type == "reward-redeemed"
            ):
                return

            # Resolve the streamer once: the list can shrink or reorder while
            # this message is handled, so never index into it again below.
            streamer = get_streamer(ws.streamers, message.channel_id)
            if streamer is not None:
                try:
                    if message.topic == "community-points-user-v1":
                        track_points_for_streamer = (
                            not is_drop_discovered(streamer)
                            or streamer.explicitly_configured is True
                            or Settings.track_category_streamer_points is True
                        )

                        if message.type in ["points-earned", "points-spent"]:
                            if track_points_for_streamer is not True:
                                return

                            balance = message.data["balance"]["balance"]
                            streamer.channel_points = balance
                            # Analytics switch
                            if Settings.enable_analytics is True:
                                streamer.persistent_series(
                                    event_type=(
                                        message.data["point_gain"]["reason_code"]
                                        if message.type == "points-earned"
                                        else "Spent"
                                    )
                                )

                        if message.type == "points-earned":
                            earned = message.data["point_gain"]["total_points"]
                            reason_code = message.data["point_gain"]["reason_code"]

                            logger.info(
                                f"+{earned} → {streamer} - Reason: {reason_code}.",
                                extra={
                                    "emoji": ":rocket:",
                                    "event": Events.get(f"GAIN_FOR_{reason_code}"),
                                },
                            )
                            streamer.update_history(
                                reason_code, earned, awarded_at=_sent_at(message)
                            )
                            WebSocketsPool._note_prediction_credit(
                                ws, streamer, reason_code, earned, _sent_at(message)
                            )
                            # Analytics switch
                            if Settings.enable_analytics is True:
                                streamer.persistent_annotations(
                                    reason_code, f"+{earned} - {reason_code}"
                                )
                        elif message.type == "claim-available":
                            ws.twitch.claim_bonus(
                                streamer,
                                message.data["claim"]["id"],
                            )

                    elif message.topic == "video-playback-by-id":
                        # There is stream-up message type, but it's sent earlier than the API updates
                        if message.type == "stream-up":
                            streamer.stream_up = time.time()
                        elif message.type == "stream-down":
                            # The bump makes a refresh whose requests are out
                            # drop its reply, which may predate this
                            # stream-down, instead of putting the ended
                            # broadcast back online. The rest runs under the
                            # state lock, deferred if a refresh is applying
                            # its reply right now.
                            bump_streamer_state(streamer.username)
                            run_under_state_lock(
                                streamer.username,
                                functools.partial(
                                    _apply_stream_down,
                                    message,
                                    streamer,
                                    time.time(),
                                ),
                                "stream-down",
                            )
                        elif message.type == "viewcount":
                            if streamer.stream_up_elapsed():
                                _check_online_in_background(ws.twitch, streamer)

                    elif message.topic == "raid":
                        if message.type == "raid_update_v2":
                            raid = Raid(
                                message.message["raid"]["id"],
                                message.message["raid"]["target_login"],
                            )
                            ws.twitch.update_raid(streamer, raid)

                    elif message.topic == "community-moments-channel-v1":
                        if message.type == "active":
                            ws.twitch.claim_moment(streamer, message.data["moment_id"])

                    elif message.topic == "predictions-channel-v1":

                        event_dict = message.data["event"]
                        event_id = event_dict["id"]
                        event_status = event_dict["status"]

                        current_tmsp = parser.parse(message.timestamp)

                        if (
                            message.type == "event-created"
                            and event_id not in ws.events_predictions
                        ):
                            if event_status == "ACTIVE":
                                prediction_window_seconds = float(
                                    event_dict["prediction_window_seconds"]
                                )
                                # Reduce prediction window by 3/6s - Collect more accurate data for decision
                                prediction_window_seconds = (
                                    streamer.get_prediction_window(
                                        prediction_window_seconds
                                    )
                                )
                                event = EventPrediction(
                                    streamer,
                                    event_id,
                                    event_dict["title"],
                                    parser.parse(event_dict["created_at"]),
                                    prediction_window_seconds,
                                    event_status,
                                    event_dict["outcomes"],
                                )
                                if (
                                    streamer.is_online
                                    and event.closing_bet_after(current_tmsp) > 0
                                ):
                                    bet_settings = streamer.settings.bet
                                    if (
                                        bet_settings.minimum_points is None
                                        or streamer.channel_points
                                        > bet_settings.minimum_points
                                    ):
                                        ws.events_predictions[event_id] = event
                                        start_after = event.closing_bet_after(
                                            current_tmsp
                                        )

                                        if not WebSocketsPool._schedule_prediction(
                                            ws,
                                            start_after,
                                            ws.events_predictions[event_id],
                                        ):
                                            return

                                        logger.info(
                                            f"Place the bet after: {start_after}s for: {ws.events_predictions[event_id]}",
                                            extra={
                                                "emoji": ":alarm_clock:",
                                                "event": Events.BET_START,
                                            },
                                        )
                                    else:
                                        logger.info(
                                            f"{streamer} have only {streamer.channel_points} channel points and the minimum for bet is: {bet_settings.minimum_points}",
                                            extra={
                                                "emoji": ":pushpin:",
                                                "event": Events.BET_FILTERS,
                                            },
                                        )

                        elif (
                            message.type == "event-updated"
                            and event_id in ws.events_predictions
                        ):
                            ws.events_predictions[event_id].status = event_status
                            # Game over we can't update anymore the values... The bet was placed!
                            if (
                                ws.events_predictions[event_id].bet_placed is False
                                and ws.events_predictions[event_id].bet.decision == {}
                            ):
                                ws.events_predictions[event_id].bet.update_outcomes(
                                    event_dict["outcomes"]
                                )

                    elif message.topic == "predictions-user-v1":
                        event_id = message.data["prediction"]["event_id"]
                        if event_id in ws.events_predictions:
                            event_prediction = ws.events_predictions[event_id]
                            if message.type == "prediction-result":
                                # The result names the bet too: its outcome,
                                # and its points are the bet's final total.
                                # It confirms the bet even when the earlier
                                # messages were missed (Twitch sends it only
                                # for the account's own bets).
                                event_prediction.confirm_bet(
                                    message.data["prediction"], final=True
                                )
                                WebSocketsPool._report_prediction_result(
                                    streamer,
                                    event_prediction,
                                    message.data["prediction"]["result"],
                                )
                            elif message.type == "prediction-made":
                                event_prediction.confirm_bet(message.data["prediction"])
                                history_journal.JOURNAL.record_event(
                                    streamer,
                                    "PREDICTION_MADE",
                                    WebSocketsPool._prediction_made_label(
                                        event_prediction, message.data
                                    ),
                                )
                                # Analytics switch
                                if Settings.enable_analytics is True:
                                    choice = event_prediction.placed_choice()
                                    streamer.persistent_annotations(
                                        "PREDICTION_MADE",
                                        (
                                            f"Decision: {choice} - {event_prediction.title}"
                                            if choice is not None
                                            else f"{event_prediction.title}"
                                        ),
                                    )
                            elif message.type == "prediction-updated":
                                # More points added to the bet, by hand or by
                                # the miner.
                                event_prediction.confirm_bet(
                                    message.data["prediction"], addition=True
                                )
                    elif message.topic == "community-points-channel-v1":
                        if message.type == "community-goal-created":
                            # TODO Untested, hard to find this happening live
                            streamer.add_community_goal(
                                CommunityGoal.from_pubsub(
                                    message.data["community_goal"]
                                )
                            )
                        elif message.type == "community-goal-updated":
                            streamer.update_community_goal(
                                CommunityGoal.from_pubsub(
                                    message.data["community_goal"]
                                )
                            )
                        elif message.type == "community-goal-deleted":
                            # TODO Untested, not sure what the message format for this is,
                            #      https://github.com/sammwyy/twitch-ps/blob/master/main.js#L417
                            #      suggests that it should be just the entire, now deleted, goal model
                            streamer.delete_community_goal(
                                message.data["community_goal"]["id"]
                            )

                        if message.type in [
                            "community-goal-updated",
                            "community-goal-created",
                        ]:
                            ws.twitch.contribute_to_community_goals(streamer)

                except Exception:
                    logger.error(
                        f"Exception raised for topic: {message.topic} and message: {message}",
                        exc_info=True,
                    )

        elif response["type"] == "RESPONSE" and len(response.get("error", "")) > 0:
            # raise RuntimeError(f"Error while trying to listen for a topic: {response}")
            error_message = response.get("error", "")
            logger.error(f"Error while trying to listen for a topic: {error_message}")

            # Check if the error message indicates an authentication issue (ERR_BADAUTH)
            if "ERR_BADAUTH" in error_message:
                # Inform the user about the potential outdated cookie file
                username = ws.twitch.twitch_login.username
                logger.error(
                    f'Received the ERR_BADAUTH error, most likely you have an outdated cookie file "cookies\\{username}.pkl". Delete this file and try again.'
                )
                # Attempt to delete the outdated cookie file
                # try:
                #     cookie_file_path = os.path.join("cookies", f"{username}.pkl")
                #     if os.path.exists(cookie_file_path):
                #         os.remove(cookie_file_path)
                #         logger.info(f"Deleted outdated cookie file for user: {username}")
                #     else:
                #         logger.warning(f"Cookie file not found for user: {username}")
                # except Exception as e:
                #     logger.error(f"Error occurred while deleting cookie file: {str(e)}")

        elif response["type"] == "RECONNECT":
            logger.info(f"#{ws.index} - Reconnection required")
            WebSocketsPool.handle_reconnection(ws)

        elif response["type"] == "PONG":
            ws.last_pong = time.time()
