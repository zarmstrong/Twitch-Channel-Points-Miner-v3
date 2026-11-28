# History

[Back to the README](../README.md)

The optional **History** feature adds durable, account-scoped statistics to the
analytics server: watch sessions and watch time, live sessions, points gained
and spent, watch streaks and prediction events. It serves them through a JSON
API: daily totals, an activity heatmap, streamer totals and a per-streamer
drill-down, and the dashboard's **History** tab shows them.

History runs only when `MINER_CONFIG["enable_analytics"]` is `True`, and is
served by the analytics server (`ANALYTICS_CONFIG`).

## How it records

While it mines, the miner writes down what it does, as it happens, in a
journal: one line per fact.

- **Watch time**: each minute-watched request Twitch accepted, with the time
  since the previous request for that channel. Time while requests failed or
  were skipped (for example, before the stream's spade URL loaded) is not
  counted.
- **Points**: each points message Twitch sent, with the amount earned.
- **Spends**: channel-point redemptions (with the reward's cost), the miner's
  own bets and the miner's own community-goal contributions.
- **Streams**: a channel going live or offline, and title or game changes.
- **Streaks**: each watch-streak award Twitch sent.
- **Events**: prediction results and placed predictions.

History then adds the journal up in its own thread every few seconds. Each
journal line has a unique position, so History never counts a line twice: a
restart, a crash or a database error in the middle of an import cannot change
the totals.

History records the streamers the miner tracks for their own sake: configured
and followed streamers. Channels it tracks only to farm Drops (category,
wildcard-category and badge discovery) are not recorded. A channel you also
configure is recorded as usual. Removing a channel from the configuration, or
the miner keeping it only for Drops, ends its History from then on; what was
recorded stays.

History also imports, once, what the existing per-streamer analytics data
holds from before it started recording: see
[Importing existing analytics data](#importing-existing-analytics-data).

## Importing existing analytics data

The analytics data the miner already keeps for the Points tab
(`analytics/<username>/<streamer>.json`) holds the channel-points balance after
each points message, plus watch streaks and predictions. When History starts
recording, it adds what that data holds from before the journal began, so an
account that has mined for months doesn't start with an empty History tab.

- **What is imported:** points gained (from the change between two balances),
  spends recorded as `Spent`, watch streaks, and placed and finished
  predictions. Watch time and live sessions are not in the analytics data, so
  the imported days have none. The History tab says which days were imported.
- **Only before History:** only entries from before the second of the first
  journal record History holds are imported. Everything after it comes from
  the journal, so nothing is counted twice.
- **Which streamers:** only the streamers History records, as the miner lists
  them once it has loaded its streamers. Channels it tracks only for Drops,
  and streamers no longer in the configuration, are not imported. A streamer
  added to the configuration later is imported then.
- **Once:** each streamer is imported once per database, in small steps in
  History's own thread, so a large analytics folder doesn't slow down startup
  or mining. A file that can't be read is tried again later. One that isn't
  valid JSON is skipped, with a warning.
- **Deletes:** a streamer whose History was deleted is never imported, also
  not after the database is rebuilt.
- **Rebuilds:** a rebuilt database imports the analytics data again, up to the
  first journal record it was rebuilt from. That fills in what the rebuild
  lost, as far as the analytics data reaches.
- **Retention:** imported rows follow the same retention window as recorded
  ones. Rows older than it are pruned as soon as the import finishes, and
  their daily totals are kept.

To turn the automatic import off, set
`ANALYTICS_CONFIG["history_import_analytics"]` to `False`, or clear **Import
existing analytics data automatically** in the Config tab's **History** box.

With it off, the same box shows **Import existing analytics data** while some
streamer's analytics data hasn't been imported. The button runs the import
once, with the same rules as above. The request is saved in the database, so
a restart finishes it. Once every streamer History records has been imported,
the button is hidden and the box says so. A streamer added to the
configuration later shows the button again; it isn't imported until it is
clicked. With the automatic import on, the box shows the import's progress
instead of the button. While the miner is stopped (the Windows app can keep
the dashboard open), History takes no import request, and the box says so.

`GET /history/import` reports whether the import is done (`complete`), and
`POST /history/import` starts it. `GET /history/health` reports the same under
`analytics_import`.

## Storage

Everything lives in the account's analytics folder, beside the existing
analytics files:

- `analytics/<username>/history/` holds the journal: `r<run>-<segment>.jsonl`
  files, a run counter, `imported` (the last file History has imported) and
  `deleted` (where each [streamer delete](#deleting-a-streamer) took effect,
  so a rebuilt database does not bring deleted History back).
  Each start of the miner is a new run. A file is closed at 4 MB, and the miner
  deletes closed files once History has imported them. If History cannot
  import for a long time, the miner keeps at most 64 MB and then deletes the
  oldest files, which History reports as gaps.
- `analytics/<username>/history.sqlite3` holds what History has imported.

The database uses SQLite WAL mode, indexed range queries and small daily
rollups. Raw rows are kept for 730 days by default. Set
`ANALYTICS_CONFIG["history_retention_days"]`, or **Keep raw rows** in the
Config tab's **History** box, to change the window; `0` keeps raw rows
forever. A change takes effect after a restart. The
`TCPM_HISTORY_RETENTION_DAYS` environment variable overrides the configured
value, and the Config tab says so when it is set. Daily rollups are kept
forever, so lifetime totals remain available after raw rows are pruned.

Deleting the database while the miner is stopped makes History rebuild it
from the journal files still on disk at the next start. Those are usually only
the most recent ones, so a rebuild doesn't bring back older watch time or live
sessions. Older points, spends, watch streaks and predictions come back from
the analytics data, if it still holds them: see
[Importing existing analytics data](#importing-existing-analytics-data).

### Docker

Docker users should keep the existing `analytics` volume. With the documented
Compose setup, `./analytics` is mounted at `/usr/src/app/analytics`, so the
journal and database live in `/usr/src/app/analytics/<username>/`. They are
deleted with the volume; use a persistent bind mount or named volume if you
want to keep History across container replacement.

### Network shares

SQLite locking and WAL are unreliable on network shares such as NFS and SMB.
Keep the analytics directory on local disk. If the database is on a network
share, expect lock errors, slow queries or a database rebuild. History logs a
warning and retries or rebuilds; mining and the existing points chart
continue. A failure that keeps repeating logs its traceback at most once a
minute.

## Concurrency

- The miner's threads append to the journal under one lock. Writing never
  stops mining: a failed write is logged (at most once a minute) and that line
  is lost.
- History is the only normal writer of the database. Deleting a streamer from
  the dashboard is the only writer that runs on a request thread; both are
  serialized by a process-local lock.
- Every database operation opens a short-lived connection. No SQLite
  connection is shared between threads, and readers open the database
  read-only. WAL mode lets them query while History writes.
- `busy_timeout` is set to five seconds so an external process holding a lock
  gets a chance to finish.

## Recovery

The schema version is stored in `PRAGMA user_version`. If the file was written
by a different schema than the running build uses (including databases from
development builds of this feature), or SQLite reports that it is corrupt, the
file and its `-wal`/`-shm` companions are moved to a
`history.sqlite3.bak-<timestamp>` backup and a fresh database is created from
the journal files on disk. Locked or unwritable databases are left alone;
History logs a warning and retries without affecting the miner.

`GET /history/health` reports the import position and counts problems:

- `gaps`: journal lines lost, for example the last lines of a crash or files
  the size limit deleted before History imported them.
- `damaged_lines`: lines that could not be read, such as a line cut off by a
  crash.
- `skipped_segments`: journal files that could not be read for over an hour
  and were skipped.
- `missing_segments`: files deleted before History finished them, including
  whole miner runs the size limit deleted while History was not running. It is
  a lower bound: History counts one file for each missing run. A run whose
  journal could not be opened at all also counts as one.

## Deleting a streamer

There are two ways to delete a streamer's History from the dashboard:

- **Delete history** in a streamer's drill-down on the History tab deletes only
  their History: points, spends, predictions, watch and live sessions, watch
  streaks, and their daily and all-time totals. Their analytics data and the
  Points tab are not changed.
- Deleting a streamer's analytics data on the Points tab also clears that
  streamer's History.

Neither removes the streamer from your miner configuration. For a streamer
still in it, what the miner does after the delete is recorded again, so the
streamer comes back to History with their new activity. For a streamer you
have already removed from the configuration, the delete is the final cleanup.

In both cases, everything the miner journalled for that streamer before
the delete stays deleted, including lines History had not imported yet;
anything after it is recorded as usual, also after the database is rebuilt
from the journal. If History cannot be cleared, for
example while it is still starting or because another process holds a lock on
the database, the delete returns an error and changes nothing, so you can retry
it; a delete from the Points tab then keeps the analytics data too. The delete
takes effect when History saves it beside the journal; if the database fails
only after that (for example because the disk is full), the delete still
returns an error, and History finishes clearing the database before it imports
anything else. If the save itself fails but the disk kept part of it, History
may finish the delete after a restart; it then removes only what was recorded
before the delete, never anything imported since.

A delete waits for History to finish what it is importing. If History has
not answered after 30 seconds, the History tab says the delete may still
finish, shows what is there now and lets you delete again; it still updates
when the delete finishes.

## Watch streaks

The streamer list shows the last streak award and, for a live streamer,
whether the current stream's streak was awarded. A broadcast that starts less
than 30 minutes after the previous one went offline is treated as the same
stream, matching Twitch's watch-streak rules.

## Limitations

- Watch time is what the miner credited for accepted minute-watched requests,
  not what Twitch counted.
- Watch seconds count on the UTC day of the computer's clock when they were
  credited; after a clock change some may land on a neighbouring day. Points
  and spends count on the day of Twitch's timestamp.
- A streak award is matched to the stream it was earned on by comparing
  Twitch's time with the computer's, as the miner does when it credits a
  streak. After the computer's clock was off by more than two minutes, an
  award may not be matched to a stream: it is still counted, but the
  streamer list does not show it as the live stream's streak.
- A reward or streak award whose points message Twitch never sent is not
  counted. In eight days of one miner's logs this happened to 7 of about 4,770
  rewards.
- Spends are counted only for redemptions, the miner's own bets and the
  miner's own community-goal contributions. A bet or a goal contribution made
  by hand in the browser is not counted. Stopping the miner cancels bets
  still waiting to be placed and starts no new bet or contribution; one
  still being made 30 seconds after the miner began stopping is not counted
  (the miner logs a warning).
- A miner crash can lose the last journal lines not yet written to disk.
- If History cannot import while the journal grows past 64 MB, the oldest
  unimported lines are deleted (reported as gaps). A journal file unreadable
  for over an hour is skipped (reported).
- A points message Twitch repeated two or more runs later would be counted
  again, and one without a timestamp is not checked for repeats. Neither was
  seen in eight days of logs.
- For a run that crashed, the "active" and "live" badges stop once its newest
  journal line is three minutes old.
- Imported points are worked out from the balances in the analytics data. A
  change made while the miner was not recording it, such as points earned or
  spent in the browser between two entries, is counted with the next entry
  when it fits that entry (a gain on an earning entry, a loss on a `Spent`
  one). Otherwise only the balance is kept. A bet on a prediction is counted
  only if the analytics data recorded it as `Spent`.
- The analytics data keeps whole seconds, so entries in the same second as
  History's first journal record can't be told apart from what the journal
  recorded, and aren't imported. That's at most one second of points per
  streamer.

## API

The endpoints use the analytics server's existing authentication. All of them
are read-only except `DELETE /history/streamer/<name>`. They return `503` until
History has opened its database. `days=N` covers today and the `N - 1` UTC days
before it, so `days=1` is today only.

| Endpoint | Purpose |
| --- | --- |
| `GET /history/overview?days=30` | Window and lifetime totals; `imported_before` is when the imported analytics data ends, if any was imported |
| `GET /history/streamers?days=30` | Streamer list and live streak state |
| `GET /history/streamer/<name>?days=90` | Per-streamer drill-down: daily totals for the window, and the newest 100 watch sessions, streams, events and points messages in it |
| `GET /history/daily?days=30&streamer=<name>` | Daily chart series |
| `GET /history/sessions?kind=watch\|stream\|run` | Recent sessions |
| `GET /history/events?type=<type>` | Recent events |
| `GET /history/activity?days=30` | Activity heatmap data |
| `GET /history/health` | Database readiness, the journal import status and the analytics data import's progress (`analytics_import`); `recording` is `false` while History is not running |
| `DELETE /history/streamer/<name>` | Delete one streamer's History, as described in [Deleting a streamer](#deleting-a-streamer) |
