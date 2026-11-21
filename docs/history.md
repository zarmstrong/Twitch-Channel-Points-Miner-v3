# History

[Back to the README](../README.md)

The optional **History** feature adds durable, account-scoped statistics to the
analytics server. It records live sessions, watch sessions, point changes and
streak or prediction events, and serves them through a JSON API: daily totals,
an activity heatmap, streamer totals and per-streamer drill-down. A History tab
for the dashboard that shows this data is a separate change; until it ships,
the data is only available through the [API](#api).

History is opt-in with analytics: it only runs when
`MINER_CONFIG["enable_analytics"]` is `True`. It runs as a passive recorder.
The miner keeps writing its existing per-streamer JSON, and the recorder imports
that data without changing the files. Existing point and annotation history is
backfilled on first start. Watch time starts accumulating when History is first
enabled because the old JSON does not contain sessions.

## Storage

Data is stored in `analytics/<username>/history.sqlite3`, beside the existing
analytics files. The database is derived data: the per-streamer JSON remains
the source of truth and the database can be deleted and rebuilt. The JSON only
contains points and annotations, so a rebuild cannot recover watch sessions
that existed only in the database.

The database uses SQLite WAL mode, indexed range queries and small daily
rollups. Raw rows are kept for 730 days by default. Set
`ANALYTICS_CONFIG["history_retention_days"]` to change the window; `0` keeps raw
rows forever. The `TCPM_HISTORY_RETENTION_DAYS` environment variable overrides
the configured value. Daily rollups are kept forever, so lifetime totals remain
available after raw rows are pruned.

### Docker

Docker users should keep the existing `analytics` volume. With the documented
Compose setup, `./analytics` is mounted at `/usr/src/app/analytics`, so the
database lives at
`/usr/src/app/analytics/<username>/history.sqlite3`. It is deleted with the
volume; use a persistent bind mount or named volume if you want to keep History
across container replacement.

### Network shares

SQLite locking and WAL are unreliable on network shares such as NFS and SMB.
Keep the analytics directory on local disk. If the database is on a network
share, expect lock errors, slow queries or a database rebuild. History logs a
warning and pauses or rebuilds; mining and the existing points chart continue.

## Concurrency

Every operation opens a short-lived connection and closes it immediately. No
SQLite connection is shared between threads, and the code does not use
`check_same_thread=False`.

- The recorder is the normal miner-side writer.
- Deleting a streamer's analytics from the dashboard is the only writer that
  runs on a request thread.
- Both use the same store and are serialized by a process-local write lock, so
  only one write runs at a time.
- Readers open the database read-only. WAL mode lets them query while the
  recorder writes.
- `busy_timeout` is set to five seconds so an external process holding a lock
  gets a chance to finish.

## Schema changes and recovery

The schema version is stored in `PRAGMA user_version`. A newer build migrates
an older database in place. If the file was written by a newer schema than the
running build understands, or SQLite reports that it is corrupt, the file and
its `-wal`/`-shm` companions are moved to a `history.sqlite3.bak-<timestamp>`
backup and a fresh database is created. Locked or unwritable databases are left
alone; History logs a warning and retries without affecting the miner.

Deleting a streamer's analytics data from the dashboard also clears that
streamer's History rows. History remembers when the delete happened and does
not import anything older again, even though the watch-streak cache may still
hold it. Data recorded after the delete is kept as usual.

## Watch streaks

The streamer list shows the last streak claim and, for a live streamer, whether
the current stream's streak was claimed. A broadcast that starts less than 30
minutes after the previous one ended is treated as the same stream, matching
Twitch's watch-streak rules, so a continuation is not shown as unclaimed.

## API

The endpoints use the analytics server's existing authentication. All of them
are read-only except `DELETE /history/streamer/<name>`. They return `503` until
the recorder has initialized the database. `days=N` covers today and the
`N - 1` UTC days before it, so `days=1` is today only.

| Endpoint | Purpose |
| --- | --- |
| `GET /history/overview?days=30` | Window and lifetime totals |
| `GET /history/streamers?days=30` | Streamer list and live streak state |
| `GET /history/streamer/<name>?days=90` | Per-streamer drill-down |
| `GET /history/daily?days=30&streamer=<name>` | Daily chart series |
| `GET /history/sessions?kind=watch\|stream\|run` | Recent sessions |
| `GET /history/events?type=<type>` | Recent events |
| `GET /history/activity?days=30` | Activity heatmap data |
| `GET /history/health` | Database readiness; `recording` is `false` while the recorder is not running |
| `DELETE /history/streamer/<name>` | Delete one streamer's History rows, as described in [Schema changes and recovery](#schema-changes-and-recovery) |
