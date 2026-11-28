"""History's drops and per-game totals: journal, import, queries, the
one-time import of the Drops data, the API and the miner's call sites."""

import importlib
import json
import os
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from TwitchChannelPointsMiner.history import journal as journal_module
from TwitchChannelPointsMiner.history.backfill import AnalyticsBackfill
from TwitchChannelPointsMiner.history.importer import JournalImporter
from TwitchChannelPointsMiner.history.journal import (
    DROP_CHANNELS_KEPT,
    DROP_CLAIMS_KEPT,
    Journal,
    list_segments,
    read_deleted_drops,
    save_delete_cutoff,
    save_deleted_drops,
    segment_name,
)
from TwitchChannelPointsMiner.history.recorder import HistoryRecorder
from TwitchChannelPointsMiner.history.store import (
    _SCHEMA,
    HISTORY_SCHEMA_VERSION,
    SECONDS_PER_DAY,
    HistoryStore,
    utc_day,
)

T0 = 1_800_000_000.0


class Folder(object):
    """The account's analytics folder: the journal, written by hand so each
    case controls every field, and the Drops data."""

    def __init__(self, root):
        self.root = str(root)
        self.journal = os.path.join(self.root, "history")
        os.makedirs(self.journal, exist_ok=True)
        self.seq = {}

    def add(self, kind, at=T0, run=1, **fields):
        seq = self.seq.get(run, 0) + 1
        self.seq[run] = seq
        record = {"v": 1, "run": run, "seq": seq, "at": at, "kind": kind}
        record.update(fields)
        with open(os.path.join(self.journal, segment_name(run, 1)), "ab") as handle:
            handle.write((json.dumps(record) + "\n").encode())

    def drop(self, at=T0, run=1, drop_id="d1", claim_key=None, **fields):
        record = {
            "drop_id": drop_id,
            "claim_key": claim_key or f"i-{drop_id}|{drop_id}",
            "name": "Reward",
            "benefits": ["Badge"],
            "campaign": "Campaign",
            "game": "Game A",
        }
        record.update(fields)
        self.add("drop", at=at, run=run, **record)

    def drops_data(self, entries):
        with open(
            os.path.join(self.root, "drops_by_category.json"), "w", encoding="utf-8"
        ) as handle:
            json.dump({"drops": entries}, handle)


def captured(at, drop_id="d1", category="Game A", **fields):
    entry = {
        "x": round(at * 1000),
        "category": category,
        "campaign": "Campaign",
        "item_name": "Reward",
        "benefit": "Badge",
        "streamer": None,
        "drop_id": drop_id,
        "status": "captured",
        "minutes_required": 60,
    }
    entry.update(fields)
    return entry


@pytest.fixture
def folder(tmp_path):
    return Folder(tmp_path)


@pytest.fixture
def store(folder):
    instance = HistoryStore(os.path.join(folder.root, "history.sqlite3"))
    instance.initialize()
    return instance


@pytest.fixture
def importer(store, folder):
    return JournalImporter(store, folder.journal, monotonic=lambda: 0.0)


def backfill_for(store, folder, importer):
    return AnalyticsBackfill(
        store, folder.root, importer, lambda: [], monotonic=lambda: 0.0
    )


def rows(store, sql, *params):
    with closing(sqlite3.connect(store.path)) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(sql, params)]


def game_totals(store):
    return {
        (row["game"], row["streamer"]): (row["watch_seconds"], row["points_gained"])
        for row in rows(
            store,
            "SELECT game, streamer, SUM(watch_seconds) AS watch_seconds, "
            "SUM(points_gained) AS points_gained FROM daily_game_stats "
            "GROUP BY game, streamer",
        )
    }


# -- journal -------------------------------------------------------------------
@pytest.fixture
def journal(tmp_path, monkeypatch):
    writer = Journal(monotonic=lambda: 0.0, clock=lambda: T0)
    writer.start(str(tmp_path / "journal"))
    monkeypatch.setattr(journal_module, "JOURNAL", writer)
    yield writer
    writer.stop()


def written(journal, kind):
    found = []
    for name in list_segments(journal.folder):
        with open(os.path.join(journal.folder, name), "rb") as handle:
            found += [json.loads(raw) for raw in handle.read().split(b"\n") if raw]
    return [record for record in found if record["kind"] == kind]


def test_a_claim_is_credited_to_the_channel_twitch_last_reported(journal):
    journal.note_drop_channel(SimpleNamespace(username="Alice"), "d1")
    journal.note_drop_channel("bob", "d1")
    journal.note_drop_channel("carol", "d2")
    journal.record_drop(
        "d1",
        "i1|d1",
        name=" Reward ",
        benefits=["Badge", "Badge", "", None, "Emote"],
        campaign="Campaign",
        game="Game A",
        minutes_required=60,
    )
    journal.record_drop("d3", "i3|d3", game="Unknown", quest=True)

    first, second = written(journal, "drop")
    assert first["streamer"] == "bob"
    assert first["name"] == "Reward"
    assert first["benefits"] == ["Badge", "Emote"]
    assert first["game"] == "Game A"
    assert first["quest"] is False
    assert first["minutes_required"] == 60
    # No progress seen this run, and an "Unknown" category is no game.
    assert second["streamer"] is None
    assert second["game"] is None
    assert second["quest"] is True


def test_only_the_newest_drop_channels_are_kept(journal):
    for index in range(DROP_CHANNELS_KEPT + 1):
        journal.note_drop_channel("alice", f"d{index}")
    journal.note_drop_channel("bob", "d1")
    assert journal.drop_channel("d0") is None
    assert journal.drop_channel("d1") == "bob"
    assert journal.drop_channel(f"d{DROP_CHANNELS_KEPT}") == "alice"


def test_a_claim_is_written_once_per_process(journal, monkeypatch):
    journal.record_drop("d1", "i1|d1")
    journal.record_drop("d1", "i1|d1", already_claimed=True)
    journal.record_drop("d1#2", "i1|d1#2")
    assert [record["claim_key"] for record in written(journal, "drop")] == [
        "i1|d1",
        "i1|d1#2",
    ]
    # However many other claims came since.
    for index in range(DROP_CLAIMS_KEPT - 2):
        journal._drop_claims[f"other-{index}"] = True
    journal.record_drop("d1", "i1|d1", already_claimed=True)
    assert len(written(journal, "drop")) == 2
    # A claim whose record could not be written is written when reported
    # again.
    monkeypatch.setattr(journal, "append", lambda kind, **fields: False)
    journal.record_drop("d2", "i2|d2")
    monkeypatch.undo()
    journal.record_drop("d2", "i2|d2")
    assert [record["claim_key"] for record in written(journal, "drop")][-1] == "i2|d2"


def test_a_claim_written_without_its_game_is_written_again_with_it(
    journal, folder, store, monkeypatch
):
    journal.record_drop("d1", "i1|d1", game="Unknown")
    journal.record_drop("d1", "i1|d1", game="Game A", already_claimed=True)
    journal.record_drop("d1", "i1|d1", game="Game B", already_claimed=True)
    journal.record_drop("d1", "i1|d1", already_claimed=True)
    assert [record["game"] for record in written(journal, "drop")] == [None, "Game A"]
    # Imported, the claim counts once, under its game.
    importer = JournalImporter(store, journal.folder, monotonic=lambda: 0.0)
    importer.poll()
    assert [
        (row["drop_id"], row["game"]) for row in rows(store, "SELECT * FROM drops")
    ] == [("d1", "Game A")]


def test_a_claim_written_without_its_channel_is_written_again_with_it(journal, store):
    journal.record_drop("d1", "i1|d1", game="Game A")
    journal.note_drop_channel("alice", "d1")
    journal.record_drop("d1", "i1|d1", game="Game A", already_claimed=True)
    journal.note_drop_channel("bob", "d1")
    journal.record_drop("d1", "i1|d1", game="Game A", already_claimed=True)
    assert [record["streamer"] for record in written(journal, "drop")] == [
        None,
        "alice",
    ]
    # Imported, the claim counts once, credited to the channel.
    importer = JournalImporter(store, journal.folder, monotonic=lambda: 0.0)
    importer.poll()
    assert [
        (row["drop_id"], row["streamer"], row["game"])
        for row in rows(store, "SELECT * FROM drops")
    ] == [("d1", "alice", "Game A")]


def test_a_claim_written_again_keeps_what_was_written_before(journal):
    journal.record_drop("d1", "i1|d1", game="Game A")
    journal.note_drop_channel("alice", "d1")
    # Learns the channel, reported without the game this time.
    journal.record_drop("d1", "i1|d1", game="Unknown", already_claimed=True)
    journal.record_drop("d1", "i1|d1", game="Game A", already_claimed=True)
    assert [
        (record["game"], record["streamer"]) for record in written(journal, "drop")
    ] == [("Game A", None), ("Game A", "alice")]


def test_a_channel_noted_after_the_claim_is_written_to_it(journal, store):
    # Claimed right after a restart, before any progress named a channel;
    # a session still on the drop names it later.
    journal.record_drop("d1", "i1|d1", name="Reward", game="Game A")
    journal.record_drop("d2", "i2|d2", game="Game B")
    journal.note_drop_channel("alice", "d1")
    journal.note_drop_channel("bob", "d1")
    journal.note_drop_channel("carol", "d3")
    assert [
        (record["drop_id"], record["streamer"], record["game"], record["name"])
        for record in written(journal, "drop")
    ] == [
        ("d1", None, "Game A", "Reward"),
        ("d2", None, "Game B", None),
        ("d1", "alice", "Game A", "Reward"),
    ]
    importer = JournalImporter(store, journal.folder, monotonic=lambda: 0.0)
    importer.poll()
    assert sorted(
        (row["drop_id"], row["streamer"] or "")
        for row in rows(store, "SELECT * FROM drops")
    ) == [("d1", "alice"), ("d2", "")]


def test_a_channel_noted_while_the_rewrite_fails_is_written_later(journal, monkeypatch):
    journal.record_drop("d1", "i1|d1", game="Game A")
    monkeypatch.setattr(journal, "append", lambda kind, **fields: False)
    journal.note_drop_channel("alice", "d1")
    monkeypatch.undo()
    journal.note_drop_channel("alice", "d1")
    assert [record["streamer"] for record in written(journal, "drop")] == [
        None,
        "alice",
    ]


def test_an_unwritten_claim_keeps_what_twitch_said_first(journal, monkeypatch):
    real_append = journal.append
    monkeypatch.setattr(journal, "append", lambda kind, **fields: False)
    journal.record_drop("d1", "i1|d1", already_claimed=True)
    monkeypatch.setattr(journal, "append", real_append)
    journal.record_drop("d1", "i1|d1")
    assert [record["already_claimed"] for record in written(journal, "drop")] == [True]


def test_a_claim_without_an_id_or_a_running_journal_is_not_written(journal):
    journal.record_drop("", "key")
    journal.record_drop("d1", "")
    assert written(journal, "drop") == []
    stopped = Journal()
    stopped.record_drop("d1", "key")
    assert stopped.running is False


# -- import ----------------------------------------------------------------------
def test_a_claim_counts_once_however_often_it_is_reported(folder, importer, store):
    folder.drop(at=T0)
    folder.drop(at=T0 + 600, run=2, already_claimed=True)
    folder.drop(
        at=T0 + 700, run=2, drop_id="d1#2", claim_key="i-d1|d1#2", quest=True, game=None
    )
    importer.poll()
    drops = rows(store, "SELECT * FROM drops ORDER BY ts")
    assert [(row["ts"], row["quest"], row["game"]) for row in drops] == [
        (T0, 0, "Game A"),
        (T0 + 700, 1, None),
    ]
    assert json.loads(drops[0]["benefits"]) == ["Badge"]
    assert drops[0]["source"] == "journal"


def test_watch_time_and_points_go_to_the_game_the_stream_showed(
    folder, importer, store
):
    folder.add("live", at=T0, streamer="alice", broadcast_id="b1", game="Game A")
    folder.add("watch", at=T0 + 20, streamer="alice", broadcast_id="b1", seconds=20)
    folder.add("points", at=T0 + 25, streamer="alice", broadcast_id="b1", delta=10)
    # The streamer switches games mid-stream.
    folder.add("live", at=T0 + 30, streamer="alice", broadcast_id="b1", game="Game B")
    folder.add("watch", at=T0 + 40, streamer="alice", broadcast_id="b1", seconds=20)
    folder.add("points", at=T0 + 45, streamer="alice", broadcast_id="b1", delta=50)
    # Not on a known stream, and a balance without an amount: no game.
    folder.add("watch", at=T0 + 60, streamer="alice", broadcast_id=None, seconds=20)
    folder.add("points", at=T0 + 65, streamer="alice", broadcast_id="b1", delta=None)
    importer.poll()
    assert game_totals(store) == {
        ("Game A", "alice"): (20.0, 10),
        ("Game B", "alice"): (20.0, 50),
    }


def test_points_earned_after_the_stream_went_offline_have_no_game(
    folder, importer, store
):
    folder.add("live", at=T0, streamer="alice", broadcast_id="b1", game="Game A")
    folder.add("points", at=T0 + 25, streamer="alice", broadcast_id="b1", delta=10)
    folder.add("offline", at=T0 + 600, streamer="alice", broadcast_id="b1")
    # Earned offline, but credited to the broadcast it was sent after.
    folder.add(
        "points",
        at=T0 + 1200,
        streamer="alice",
        broadcast_id="b1",
        delta=5,
        upstream_at=T0 + 1200,
    )
    # A late message for points earned while it was live.
    folder.add(
        "points",
        at=T0 + 1300,
        streamer="alice",
        broadcast_id="b1",
        delta=7,
        upstream_at=T0 + 590,
    )
    importer.poll()
    assert game_totals(store) == {("Game A", "alice"): (0, 17)}


def test_points_earned_while_a_stream_was_offline_between_lives_have_no_game(
    folder, importer, store
):
    folder.add("live", at=T0, streamer="alice", broadcast_id="b1", game="Game A")
    folder.add("offline", at=T0 + 600, streamer="alice", broadcast_id="b1")
    folder.add("live", at=T0 + 1800, streamer="alice", broadcast_id="b1", game="Game A")
    # Late messages, sent once it was live again.
    for earned, delta in ((T0 + 300, 1), (T0 + 1200, 10), (T0 + 2400, 100)):
        folder.add(
            "points",
            at=T0 + 3000,
            streamer="alice",
            broadcast_id="b1",
            delta=delta,
            upstream_at=earned,
        )
    importer.poll()
    assert game_totals(store) == {("Game A", "alice"): (0, 101)}


def test_a_claim_recorded_without_its_game_learns_it(folder, importer, store):
    folder.drop(at=T0, game=None)
    importer.poll()
    folder.drops_data([captured(T0 + 60)])
    backfill_for(store, folder, importer).poll()
    folder.drop(at=T0 + 600, run=2, already_claimed=True)
    importer.poll()
    assert [
        (row["source"], row["game"]) for row in rows(store, "SELECT * FROM drops")
    ] == [("journal", "Game A")]


def test_a_claim_recorded_without_its_channel_learns_it(folder, importer, store):
    folder.drop(at=T0, game=None)
    importer.poll()
    folder.drop(at=T0 + 600, run=2, streamer="alice", already_claimed=True)
    # A channel once known is kept.
    folder.drop(at=T0 + 700, run=3, streamer="bob", already_claimed=True)
    importer.poll()
    assert [
        (row["streamer"], row["game"]) for row in rows(store, "SELECT * FROM drops")
    ] == [("alice", "Game A")]
    # So the channel's drill-down lists it.
    assert store.drops(days=7, streamer="alice", now=T0 + 60)["total"] == 1


@pytest.mark.parametrize("journal_first", [False, True])
def test_a_journalled_claim_keeps_what_the_drops_data_knew(
    folder, importer, store, journal_first
):
    # The journalled claim knew neither its game nor its channel.
    if journal_first:
        folder.drop(at=T0, game=None)
        importer.poll()
    folder.drops_data([captured(T0 + 1, streamer="alice")])
    backfill_for(store, folder, importer).poll()
    if not journal_first:
        folder.drop(at=T0, game=None)
        importer.poll()
    assert [
        (row["source"], row["game"], row["streamer"])
        for row in rows(store, "SELECT * FROM drops")
    ] == [("journal", "Game A", "alice")]


def test_a_journalled_claim_replaces_the_same_claim_from_the_drops_data(
    folder, importer, store
):
    folder.drops_data(
        [
            # Written right after the claim below: the same claim.
            captured(T0 + 1),
            # Another drop, and the same drop in another category: still
            # the same claim.
            captured(T0 + 1, drop_id="d2"),
            captured(T0 + 1, category="Game B"),
        ]
    )
    backfill_for(store, folder, importer).poll()
    assert sorted(
        (row["drop_id"], row["game"]) for row in rows(store, "SELECT * FROM drops")
    ) == [("d1", "Game A"), ("d2", "Game A")]
    folder.drop(at=T0)
    importer.poll()
    assert sorted(
        (row["drop_id"], row["game"], row["source"])
        for row in rows(store, "SELECT * FROM drops")
    ) == [
        ("d1", "Game A", "journal"),
        ("d2", "Game A", "analytics"),
    ]


@pytest.mark.parametrize("journal_first", [True, False])
def test_the_drops_data_import_and_the_journal_agree_in_either_order(
    folder, importer, store, journal_first
):
    # A claim from before History (an earlier repeat of the same drop), then
    # the journalled claim, which also rewrote the entry: the entry is the
    # journalled claim, so only it counts.
    folder.drops_data([captured(T0 + 30)])
    folder.drop(at=T0)
    backfill = backfill_for(store, folder, importer)
    if journal_first:
        importer.poll()
        backfill.poll()
    else:
        backfill.poll()
        importer.poll()
    assert [
        (row["source"], row["ts"]) for row in rows(store, "SELECT * FROM drops")
    ] == [("journal", T0)]


@pytest.mark.parametrize("journal_first", [True, False])
@pytest.mark.parametrize("entry_at", [T0 - 3 * 86400, T0 + 3 * 86400])
def test_a_drop_is_one_claim_whenever_its_entry_was_written(
    folder, importer, store, journal_first, entry_at
):
    # A drop is claimed once.  Its entry can be older than the claim (the
    # minutes completed days before the claim went through) or newer (the
    # drop was seen again since), and it is still the same claim.
    folder.drops_data([captured(entry_at)])
    folder.drop(at=T0)
    backfill = backfill_for(store, folder, importer)
    if journal_first:
        importer.poll()
        backfill.poll()
    else:
        backfill.poll()
        importer.poll()
    assert [
        (row["source"], row["ts"]) for row in rows(store, "SELECT * FROM drops")
    ] == [("journal", T0)]


def test_the_startup_scrape_and_a_later_claim_count_once(folder, importer, store):
    # The miner's startup scrape writes a "captured" entry for a drop whose
    # minutes are complete; the claim goes through minutes later.
    folder.drops_data([captured(T0)])
    backfill_for(store, folder, importer).poll()
    folder.drop(at=T0 + 600)
    importer.poll()
    assert [
        (row["source"], row["ts"]) for row in rows(store, "SELECT * FROM drops")
    ] == [("journal", T0 + 600)]


def test_each_repeat_of_a_quest_is_a_claim_of_its_own(folder, importer, store):
    # A repeating reward group's repeats are drops of their own.
    folder.drops_data([captured(T0 - 86400, drop_id="g1", category="Quests")])
    folder.drop(at=T0, drop_id="g1#2", claim_key="i-g1|g1#2", game="Quests", quest=True)
    importer.poll()
    backfill_for(store, folder, importer).poll()
    assert sorted(
        (row["drop_id"], row["source"]) for row in rows(store, "SELECT * FROM drops")
    ) == [("g1", "analytics"), ("g1#2", "journal")]


def test_the_drops_data_is_imported_once(folder, importer, store):
    folder.drops_data(
        [
            captured(T0 - 100, streamer="Alice", item_art_url="https://x.test/a.png"),
            captured(T0 - 100, drop_id="d2", status="in_progress"),
            captured(T0 - 100, drop_id="d3", category="Unknown"),
            captured(T0 - 100, drop_id="d4", streamer="gone"),
            captured(T0 - 100, drop_id=None),
            "not a drop",
        ]
    )
    save_delete_cutoff(folder.journal, "gone", segment_name(1, 1), 0, T0)
    backfill = backfill_for(store, folder, importer)
    backfill.poll()
    drops = rows(store, "SELECT * FROM drops")
    assert [(row["drop_id"], row["streamer"]) for row in drops] == [("d1", "alice")]
    assert drops[0]["image"] == "https://x.test/a.png"
    assert drops[0]["minutes_required"] == 60
    assert backfill.status()["drops"] == {"done": True, "skipped": None, "added": 1}

    # Done: entries added later (claims the journal records) are not read.
    folder.drops_data([captured(T0 - 50, drop_id="d9")])
    backfill.poll()
    assert len(rows(store, "SELECT * FROM drops")) == 1


@pytest.mark.parametrize(
    "content, reason", [(None, "no file"), ("{", "invalid"), ("[]", "invalid")]
)
def test_a_missing_or_invalid_drops_file_is_skipped(
    folder, importer, store, content, reason
):
    if content is not None:
        with open(os.path.join(folder.root, "drops_by_category.json"), "w") as handle:
            handle.write(content)
    backfill = backfill_for(store, folder, importer)
    backfill.poll()
    assert backfill.status()["drops"]["skipped"] == reason


def test_the_drops_data_is_read_under_the_lock_the_miner_replaces_it_under(
    folder, importer, store, monkeypatch
):
    # On Windows the miner's replace of the file fails while it is open.
    from TwitchChannelPointsMiner.classes.Settings import DROPS_FILE_MUTEX
    from TwitchChannelPointsMiner.history import backfill as backfill_module

    folder.drops_data([captured(T0)])
    held = []
    real_open = open

    def tracking_open(path, *args, **kwargs):
        if str(path).endswith("drops_by_category.json"):
            held.append(DROPS_FILE_MUTEX.locked())
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(backfill_module, "open", tracking_open, raising=False)
    backfill_for(store, folder, importer).poll()
    assert held == [True]
    assert not DROPS_FILE_MUTEX.locked()
    assert len(rows(store, "SELECT * FROM drops")) == 1


def test_a_delete_removes_the_streamers_drops_and_game_totals(folder, importer, store):
    folder.add("live", at=T0, streamer="alice", broadcast_id="b1", game="Game A")
    folder.add("watch", at=T0 + 20, streamer="alice", broadcast_id="b1", seconds=20)
    folder.drop(at=T0 + 30, streamer="alice")
    folder.drop(at=T0 + 30, drop_id="d2", streamer=None)
    importer.poll()
    store.delete_streamer("alice", segment_name(1, 1), 10**6)
    assert game_totals(store) == {}
    assert [row["drop_id"] for row in rows(store, "SELECT * FROM drops")] == ["d2"]


def test_a_deleted_claim_reported_again_stays_deleted(folder, importer, store):
    folder.drop(at=T0, streamer="alice")
    importer.poll()
    store.delete_streamer("alice", segment_name(1, 1), 10**6)
    # A stale inventory: Twitch says the claim was already made, possibly in
    # a later run that no longer knows the channel.
    folder.drop(at=T0 + 600, streamer="alice", already_claimed=True)
    folder.drop(at=T0 + 700, run=2, already_claimed=True)
    # A new claim on the channel is recorded.
    folder.drop(
        at=T0 + 800, run=2, drop_id="d1#2", claim_key="i-d1|d1#2", streamer="alice"
    )
    importer.poll()
    assert [row["dedupe_key"] for row in rows(store, "SELECT * FROM drops")] == [
        "c:i-d1|d1#2"
    ]


def test_a_channel_with_only_drops_counts_as_a_games_streamer(folder, importer, store):
    folder.add("live", at=T0, streamer="alice", broadcast_id="b1", game="Game A")
    folder.add("watch", at=T0 + 20, streamer="alice", broadcast_id="b1", seconds=60)
    folder.drop(at=T0 + 30, streamer="alice")
    folder.drop(at=T0 + 30, drop_id="d2", streamer="dropsonly")
    folder.drop(at=T0 + 30, drop_id="d3", streamer="other", game="Game B")
    importer.poll()
    games = {
        item["game"]: item["streamers"]
        for item in store.games(days=1, now=T0 + 60)["games"]
    }
    assert games == {"Game A": 2, "Game B": 1}
    (only,) = store.games(days=1, streamer="dropsonly", now=T0 + 60)["games"]
    assert (only["game"], only["drops"], only["streamers"]) == ("Game A", 1, 1)


def test_the_drops_data_does_not_bring_back_a_deleted_claim(folder, importer, store):
    # Imported later (the import was off at the delete): the entry names no
    # channel, but it is the deleted claim, however much later it was
    # written.  Another drop's entry is imported.
    folder.drop(at=T0, streamer="alice")
    importer.poll()
    store.delete_streamer("alice", segment_name(1, 1), 10**6)
    folder.drops_data([captured(T0 + 3 * 86400), captured(T0, drop_id="d2")])
    backfill_for(store, folder, importer).poll()
    importer.poll()
    assert [
        (row["drop_id"], row["source"]) for row in rows(store, "SELECT * FROM drops")
    ] == [("d2", "analytics")]


def test_a_claim_deleted_before_it_was_imported_stays_deleted(folder, importer, store):
    # Journalled between the delete's last import and its cutoff.
    folder.drop(at=T0, streamer="alice")
    store.record_delete("alice", segment_name(1, 1), 10**6)
    importer.poll()
    assert rows(store, "SELECT * FROM drops") == []
    folder.drop(at=T0 + 600, run=2, already_claimed=True)
    importer.poll()
    assert rows(store, "SELECT * FROM drops") == []


def test_a_claim_deleted_before_it_was_imported_removes_its_drops_data_twin(
    folder, importer, store
):
    # The Drops data import added the claim without a channel; the claim
    # was journalled on alice's channel just before her delete, which ran
    # before the importer saw it.
    folder.drops_data([captured(T0), captured(T0, drop_id="d2")])
    backfill_for(store, folder, importer).poll()
    folder.drop(at=T0 + 5, streamer="alice")
    store.record_delete("alice", segment_name(1, 1), 10**6)
    importer.poll()
    assert [row["drop_id"] for row in rows(store, "SELECT * FROM drops")] == ["d2"]
    assert sorted(drop[0] for drop in read_deleted_drops(folder.journal)) == [
        "a:d1|Game A",
        "c:i-d1|d1",
    ]


def test_a_claim_deleted_before_it_was_imported_removes_its_channelless_twin(
    folder, importer, store
):
    # Journalled before its channel was known; reported again, on alice's
    # channel, just before her delete, which ran before the importer saw
    # it.
    folder.drop(at=T0, claim_key="k1")
    importer.poll()
    folder.drop(at=T0 + 5, run=2, claim_key="k2", streamer="alice")
    store.record_delete("alice", segment_name(2, 1), 10**6)
    importer.poll()
    assert rows(store, "SELECT * FROM drops") == []
    assert sorted(drop[0] for drop in read_deleted_drops(folder.journal)) == [
        "c:k1",
        "c:k2",
    ]


def recorder_for(folder, store):
    recorder = HistoryRecorder(
        store, folder.journal, monotonic=lambda: 0.0, clock=lambda: T0 + 60
    )
    recorder.backfill = AnalyticsBackfill(
        store, folder.root, recorder.importer, lambda: [], monotonic=lambda: 0.0
    )
    assert recorder._initialize()
    return recorder


def rebuild(store, folder):
    # The imported journal is gone by then.
    for name in list_segments(folder.journal):
        os.remove(os.path.join(folder.journal, name))
    assert store.recover_if_corrupt(
        sqlite3.DatabaseError("database disk image is malformed")
    )


def test_a_rebuilt_database_keeps_deleted_claims_deleted(folder, store):
    folder.drop(at=T0, streamer="alice")
    folder.drop(at=T0, drop_id="d2", streamer="bob")
    recorder = recorder_for(folder, store)
    recorder.poll_once()
    recorder.delete_streamer("alice")
    assert [row["drop_id"] for row in rows(store, "SELECT * FROM drops")] == ["d2"]
    # The Drops data names no channel.
    folder.drops_data([captured(T0 + 60), captured(T0 + 60, drop_id="d2")])
    rebuild(store, folder)
    # Twitch reports the deleted claim again, too.
    folder.drop(at=T0 + 600, run=2, already_claimed=True)
    for _ in range(3):
        recorder.poll_once()
    assert [
        (row["drop_id"], row["source"]) for row in rows(store, "SELECT * FROM drops")
    ] == [("d2", "analytics")]


def test_deleted_claims_that_cannot_be_saved_are_imported_again(
    folder, importer, store, monkeypatch
):
    from TwitchChannelPointsMiner.history import importer as importer_module

    folder.drops_data([captured(T0)])
    backfill_for(store, folder, importer).poll()
    folder.drop(at=T0 + 5, streamer="alice")
    store.record_delete("alice", segment_name(1, 1), 10**6)

    def disk_full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(importer_module, "save_deleted_drops", disk_full)
    with pytest.raises(Exception):
        importer.poll()
    # Not passed over as a damaged line: nothing changed.
    assert rows(store, "SELECT * FROM journal_health WHERE key = 'damaged_lines'") == []
    assert len(rows(store, "SELECT * FROM drops")) == 1
    monkeypatch.undo()
    importer.poll()
    assert rows(store, "SELECT * FROM drops") == []
    assert len(read_deleted_drops(folder.journal)) == 2


def test_a_saved_delete_the_database_never_committed_is_finished(
    folder, importer, store
):
    # Saved beside the journal, then the process stopped before the
    # import that removed the Drops data's copy committed.
    folder.drops_data([captured(T0), captured(T0, drop_id="d2")])
    backfill_for(store, folder, importer).poll()
    save_deleted_drops(
        folder.journal,
        [
            ("c:i-d1|d1", "d1", "Game A", T0 + 5, "journal"),
            ("a:d1|Game A", "d1", "Game A", T0, "analytics"),
        ],
        T0 + 5,
    )
    JournalImporter(store, folder.journal, monotonic=lambda: 0.0).poll()
    assert [row["drop_id"] for row in rows(store, "SELECT * FROM drops")] == ["d2"]


def test_a_claim_matched_to_a_deleted_one_stays_deleted_after_a_rebuild(folder, store):
    recorder = recorder_for(folder, store)
    folder.drops_data([captured(T0, streamer="alice")])
    recorder.poll_once()
    recorder.delete_streamer("alice")
    # Matched by its game, then reported without it after a rebuild.
    folder.drop(at=T0 + 30, already_claimed=True)
    recorder.poll_once()
    rebuild(store, folder)
    folder.drop(at=T0 + 600, run=2, already_claimed=True, game=None)
    for _ in range(3):
        recorder.poll_once()
    assert rows(store, "SELECT * FROM drops") == []


def test_a_deleted_claim_reported_with_its_game_later_stays_deleted(folder, store):
    recorder = recorder_for(folder, store)
    folder.drop(at=T0, streamer="alice", game=None)
    recorder.poll_once()
    recorder.delete_streamer("alice")
    # The game is known when Twitch reports the claim again; the Drops data
    # lists it under that game, also after a rebuild.
    folder.drop(at=T0 + 600, run=2, already_claimed=True)
    recorder.poll_once()
    folder.drops_data([captured(T0 + 600)])
    recorder.poll_once()
    assert rows(store, "SELECT * FROM drops") == []
    rebuild(store, folder)
    for _ in range(3):
        recorder.poll_once()
    assert rows(store, "SELECT * FROM drops") == []


def test_a_drops_data_copy_imported_after_a_delete_goes_with_the_claim(folder, store):
    recorder = recorder_for(folder, store)
    backfill, recorder.backfill = recorder.backfill, None
    folder.drop(at=T0, streamer="alice", game=None)
    recorder.poll_once()
    recorder.delete_streamer("alice")
    # The import, turned on later, lists the drop under its game; then the
    # claim is reported again with it.
    folder.drops_data([captured(T0 + 600)])
    recorder.backfill = backfill
    recorder.poll_once()
    assert backfill.status()["drops"]["done"]
    folder.drop(at=T0 + 900, run=2, already_claimed=True)
    recorder.poll_once()
    assert rows(store, "SELECT * FROM drops") == []


def test_a_claim_under_a_second_key_is_the_same_claim(folder, importer, store):
    folder.drop(at=T0)
    folder.drop(at=T0 + 600, run=2, claim_key="other-instance|d1")
    importer.poll()
    assert len(rows(store, "SELECT * FROM drops")) == 1


def test_a_deleted_imported_claim_reported_again_stays_deleted(folder, importer, store):
    folder.drops_data([captured(T0, streamer="alice")])
    backfill_for(store, folder, importer).poll()
    store.delete_streamer("alice", "", 0)
    # The same claim (the drop and category match), and the next repeat.
    folder.drop(at=T0 + 30, already_claimed=True)
    folder.drop(at=T0 + 3 * 86400, drop_id="d1#2", claim_key="i-d1|d1#2")
    importer.poll()
    assert [row["drop_id"] for row in rows(store, "SELECT * FROM drops")] == ["d1#2"]
    # Its own key is kept as deleted: the same claim reported again much
    # later, by a fresh importer, stays deleted.
    folder.drop(at=T0 + 9 * 86400, run=2, already_claimed=True)
    JournalImporter(store, folder.journal, monotonic=lambda: 0.0).poll()
    assert [row["drop_id"] for row in rows(store, "SELECT * FROM drops")] == ["d1#2"]


def test_retention_keeps_drops_and_game_totals(folder, importer, store):
    folder.add("live", at=T0, streamer="alice", broadcast_id="b1", game="Game A")
    folder.add("watch", at=T0 + 20, streamer="alice", broadcast_id="b1", seconds=20)
    folder.drop(at=T0 + 30)
    importer.poll()
    store.apply_retention(now=T0 + 10 * SECONDS_PER_DAY, retention_days=1)
    assert len(rows(store, "SELECT * FROM drops")) == 1
    assert game_totals(store) == {("Game A", "alice"): (20.0, 0)}


# -- schema 4 -----------------------------------------------------------------------
def test_a_schema_4_database_keeps_its_history(tmp_path, monkeypatch):
    path = str(tmp_path / "history.sqlite3")
    with closing(sqlite3.connect(path)) as connection, connection:
        for statement in _SCHEMA:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO daily_stats (day, streamer, points_gained) "
            "VALUES ('2027-01-15', 'alice', 42)"
        )
        connection.execute("PRAGMA user_version=4")
    store_module = importlib.import_module("TwitchChannelPointsMiner.history.store")
    monkeypatch.setattr(store_module.time, "time", lambda: T0)

    store = HistoryStore(path)
    store.initialize()

    assert not any(".bak-" in name for name in os.listdir(tmp_path))
    with closing(sqlite3.connect(path)) as connection:
        assert (
            connection.execute("PRAGMA user_version").fetchone()[0]
            == HISTORY_SCHEMA_VERSION
        )
    assert rows(store, "SELECT points_gained FROM daily_stats") == [
        {"points_gained": 42}
    ]
    assert store.overview(now=T0)["games_since"] == T0
    assert store.games(now=T0)["games_since"] == T0
    # A new database had games from the start.
    fresh = HistoryStore(str(tmp_path / "fresh.sqlite3"))
    fresh.initialize()
    assert fresh.overview(now=T0)["games_since"] is None


# -- queries -----------------------------------------------------------------------
def test_games_and_drops_cover_the_window(folder, importer, store):
    day = SECONDS_PER_DAY
    folder.add("live", at=T0, streamer="alice", broadcast_id="b1", game="Game A")
    folder.add("watch", at=T0 + 20, streamer="alice", broadcast_id="b1", seconds=600)
    folder.add("live", at=T0, streamer="bob", broadcast_id="b2", game="Game B")
    folder.add("watch", at=T0 + 20, streamer="bob", broadcast_id="b2", seconds=60)
    folder.add("points", at=T0 + 30, streamer="bob", broadcast_id="b2", delta=10)
    folder.drop(at=T0 + 40, streamer="bob", game="Game B")
    folder.drop(at=T0 + 50, drop_id="q1", game=None, quest=True)
    # Older than a one-day window.
    folder.drop(at=T0 - 3 * day, drop_id="old", game="Game C")
    importer.poll()

    games = store.games(days=1, now=T0 + 60)["games"]
    assert [
        (item["game"], item["watch_seconds"], item["drops"], item["quests"])
        for item in games
    ] == [
        ("Game A", 600.0, 0, 0),
        ("Game B", 60.0, 1, 0),
        (None, 0.0, 1, 1),
    ]
    assert games[1]["points_gained"] == 10
    assert games[1]["streamers"] == 1
    # The quest's channel is unknown: no streamer.
    assert games[2]["streamers"] == 0
    assert [
        item["game"]
        for item in store.games(days=1, streamer="bob", now=T0 + 60)["games"]
    ] == ["Game B"]
    assert {item["game"] for item in store.games(days=7, now=T0 + 60)["games"]} == {
        "Game A",
        "Game B",
        "Game C",
        None,
    }

    listed = store.drops(days=1, now=T0 + 60)
    assert listed["total"] == 2
    assert [drop["drop_id"] for drop in listed["drops"]] == ["q1", "d1"]
    assert listed["drops"][0]["quest"] is True
    assert listed["drops"][1]["benefits"] == ["Badge"]
    assert "dedupe_key" not in listed["drops"][0]
    assert store.drops(days=7, game="Game C", now=T0 + 60)["total"] == 1
    assert store.drops(days=7, streamer="bob", now=T0 + 60)["total"] == 1

    overview = store.overview(days=1, now=T0 + 60)
    assert overview["window"]["drops"] == 2
    assert overview["today"]["drops"] == (2 if utc_day(T0) == utc_day(T0 + 60) else 0)
    assert overview["lifetime"]["drops"] == 3

    detail = store.streamer_detail("bob", days=1, now=T0 + 60)
    assert [drop["drop_id"] for drop in detail["drops"]] == ["d1"]
    assert [item["game"] for item in detail["games"]] == ["Game B"]


# -- API ---------------------------------------------------------------------------
def test_the_games_and_drops_endpoints(folder, importer, store, monkeypatch):
    from flask import Flask

    from TwitchChannelPointsMiner.history import web as history_web

    folder.drop(at=T0)
    importer.poll()
    store_module = importlib.import_module("TwitchChannelPointsMiner.history.store")
    monkeypatch.setattr(store_module.time, "time", lambda: T0 + 60)
    monkeypatch.setattr(history_web, "get_store", lambda: store)
    app = Flask(__name__)
    history_web.register_history(app)
    client = app.test_client()

    games = client.get("/history/games?days=7").get_json()
    assert [item["game"] for item in games["games"]] == ["Game A"]
    drops = client.get("/history/drops?days=7&game=Game%20A").get_json()
    assert drops["total"] == 1
    assert client.get("/history/drops?days=7&game=Other").get_json()["total"] == 0
    assert client.get("/history/drops?game=" + "x" * 201).status_code == 400
    assert client.get("/history/drops?limit=0").status_code == 400


# -- call sites ---------------------------------------------------------------------
def claiming_twitch(monkeypatch, status="ELIGIBLE_FOR_ALL", order=None):
    from TwitchChannelPointsMiner.classes.Twitch import Twitch

    twitch = object.__new__(Twitch)
    twitch.log_drop_checks = False
    twitch.track_drop_item_art = True
    twitch.watch_reward_campaigns = {}
    twitch.gql = SimpleNamespace(
        claim_drop_rewards=lambda instance: SimpleNamespace(status=status, errors=[])
    )

    def save(self, drop, **kwargs):
        if order is not None:
            order.append(("analytics", len(written(journal_module.JOURNAL, "drop"))))

    monkeypatch.setattr(Twitch, "_Twitch__save_drop_claim_analytics", save)
    return twitch


def claimed_drop(drop_id="drop-1"):
    from TwitchChannelPointsMiner.classes.entities.Drop import Drop

    drop = Drop(
        {
            "id": drop_id,
            "name": "Reward",
            "benefitEdges": [
                {"benefit": {"name": "Badge", "imageAssetURL": "https://x.test/b.png"}}
            ],
            "requiredMinutesWatched": 60,
            "startAt": "2020-01-01T00:00:00Z",
            "endAt": "2099-01-01T00:00:00Z",
        }
    )
    drop.drop_instance_id = "instance-1"
    return drop


def test_an_accepted_claim_is_journalled_before_the_drops_data(journal, monkeypatch):
    from TwitchChannelPointsMiner.classes.entities.Campaign import Campaign

    order = []
    twitch = claiming_twitch(monkeypatch, order=order)
    campaign = SimpleNamespace(
        id="campaign-1", name="Campaign", game={"displayName": "Game A"}, drops=[]
    )
    journal.note_drop_channel("alice", "drop-1")
    assert twitch.claim_drop(claimed_drop(), campaign=campaign) is True

    (record,) = written(journal, "drop")
    assert record["claim_key"] == "instance-1|drop-1"
    assert record["streamer"] == "alice"
    assert record["game"] == "Game A"
    assert record["campaign"] == "Campaign"
    assert record["campaign_id"] == "campaign-1"
    assert record["benefits"] == ["Badge"]
    assert record["image"] == "https://x.test/b.png"
    assert record["minutes_required"] == 60
    assert record["already_claimed"] is False
    assert order == [("analytics", 1)]
    assert Campaign  # the entity module imports


def test_a_claim_without_a_campaign_keeps_the_campaign_id(journal, monkeypatch):
    twitch = claiming_twitch(monkeypatch)
    # The inventory pass: only the inventory's campaign link knows the id.
    assert (
        twitch.claim_drop(
            claimed_drop(),
            category_name_override="Game A",
            campaign_link={"campaign_id": "campaign-9", "connected": True},
        )
        is True
    )
    drop = claimed_drop("drop-2")
    drop.drop_instance_id = "instance-2"
    # A quest: its campaign's id is passed on its own.
    assert twitch.claim_drop(drop, campaign_id_override="quest-1") is True
    assert [record["campaign_id"] for record in written(journal, "drop")] == [
        "campaign-9",
        "quest-1",
    ]


def test_a_quest_records_every_reward_it_gives(journal, monkeypatch):
    from TwitchChannelPointsMiner.classes.Twitch import Twitch

    twitch = claiming_twitch(monkeypatch)
    # The Drops tab shows a quest as one row, named for its first reward.
    monkeypatch.setattr(
        Twitch,
        "_Twitch__drop_variant_entries_from_drop",
        lambda self, drop: [{"name": "Quest", "benefit": "Badge"}],
    )
    drop = claimed_drop()
    drop.benefit_edges = [
        {"benefit": {"name": "Badge"}},
        {"benefit": {"name": "Emote"}},
    ]
    assert twitch.claim_drop(drop) is True
    (record,) = written(journal, "drop")
    assert record["benefits"] == ["Badge", "Emote"]


def test_a_claim_twitch_says_was_already_made_is_journalled(journal, monkeypatch):
    twitch = claiming_twitch(monkeypatch, status="DROP_INSTANCE_ALREADY_CLAIMED")
    assert twitch.claim_drop(claimed_drop(), category_name_override="Game A") is True
    (record,) = written(journal, "drop")
    assert record["already_claimed"] is True
    assert record["game"] == "Game A"


@pytest.mark.parametrize("outcome", ["network", "errors", "refused"])
def test_an_unwritten_claim_is_journalled_even_if_its_retry_fails(
    journal, monkeypatch, outcome
):
    from TwitchChannelPointsMiner.classes import Twitch as twitch_module

    twitch = claiming_twitch(monkeypatch)
    real_append = journal.append
    monkeypatch.setattr(journal, "append", lambda kind, **fields: False)
    assert twitch.claim_drop(claimed_drop(), category_name_override="Game A")
    monkeypatch.setattr(journal, "append", real_append)
    twitch.accepted_drop_claims["instance-1"] -= (
        twitch_module.ACCEPTED_CLAIM_RETRY_SECONDS + 1
    )

    def claim(instance):
        if outcome == "network":
            raise twitch_module.RetryError("ClaimDrop", ["down"])
        if outcome == "errors":
            return SimpleNamespace(status=None, errors=["boom"])
        return SimpleNamespace(status="DROP_INSTANCE_ALREADY_FULFILLED", errors=[])

    twitch.gql = SimpleNamespace(claim_drop_rewards=claim)
    twitch.claim_drop(claimed_drop(), category_name_override="Game A")
    assert [record["claim_key"] for record in written(journal, "drop")] == [
        "instance-1|drop-1"
    ]


@pytest.mark.parametrize("retry", [False, True])
def test_a_claim_whose_record_was_not_written_is_journalled_when_seen_again(
    journal, monkeypatch, retry
):
    from TwitchChannelPointsMiner.classes import Twitch as twitch_module

    twitch = claiming_twitch(monkeypatch)
    real_append = journal.append
    monkeypatch.setattr(journal, "append", lambda kind, **fields: False)
    assert twitch.claim_drop(claimed_drop(), category_name_override="Game A")
    assert written(journal, "drop") == []
    monkeypatch.setattr(journal, "append", real_append)
    if retry:
        # Past the wait: the claim is sent again, and accepted again.
        twitch.accepted_drop_claims["instance-1"] -= (
            twitch_module.ACCEPTED_CLAIM_RETRY_SECONDS + 1
        )
    assert twitch.claim_drop(claimed_drop(), category_name_override="Game A")
    assert twitch.claim_drop(claimed_drop(), category_name_override="Game A")
    assert [record["claim_key"] for record in written(journal, "drop")] == [
        "instance-1|drop-1"
    ]


def test_a_refused_or_unlinked_claim_is_not_journalled(journal, monkeypatch):
    from TwitchChannelPointsMiner.classes.Twitch import Twitch

    twitch = claiming_twitch(monkeypatch, status="DROP_INSTANCE_NOT_ELIGIBLE")
    assert twitch.claim_drop(claimed_drop()) is False
    twitch = claiming_twitch(monkeypatch)
    twitch.gql = SimpleNamespace(
        claim_drop_rewards=lambda instance: SimpleNamespace(
            status="ELIGIBLE_FOR_ALL", errors=[], is_user_account_connected=False
        )
    )
    monkeypatch.setattr(
        Twitch,
        "_Twitch__record_unlinked_claim",
        lambda self, drop, now, **kwargs: False,
    )
    assert twitch.claim_drop(claimed_drop()) is False
    assert written(journal, "drop") == []
