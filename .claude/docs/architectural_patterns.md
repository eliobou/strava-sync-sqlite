# Architectural patterns

## Layered single-file structure

`sync.py` is intentionally one file, organized into labelled sections in dependency order:

```
Config → Logging → Database → Auth → API client → Data mapping → Sync orchestration → Entry point
```

Each section is self-contained. Do not split into multiple modules unless the file grows significantly — the cron deployment model favors a single entry point.

## Config table as runtime key/value store

Rather than writing back to `.env` or using a separate state file, runtime mutable state is stored in the `config` SQLite table (`sync.py:130`).

Keys in use: `access_token`, `refresh_token`, `token_expires_at`, `last_sync_timestamp`, `last_full_sync_timestamp`.

**Why**: allows the cron process to be stateless — it reads and writes everything from the DB in a single transaction scope. `.env` is only read, never written.

Accessor pattern used throughout (`sync.py:160`, `sync.py:165`):
- `get_config(conn, key, default=None)` — returns `None` if key absent
- `set_config(conn, key, value)` — upserts via `INSERT OR REPLACE`

## Soft delete

Activities removed from Strava are never physically deleted from the DB. They are flagged `is_deleted = 1` with an updated `updated_at` timestamp (`sync.py:451`).

**Why**: preserves historical data for Grafana queries. All queries must filter `WHERE is_deleted = 0` to exclude them. The `is_deleted` column is indexed (`sync.py:139`).

## Two-tier sync strategy

`needs_full_sync()` (`sync.py:462`) decides the mode each run:

- **Incremental**: uses Strava's `after=<unix_timestamp>` filter → 1 API call, no deletion detection
- **Full**: no `after` filter, paginates all pages → detects deletions by set difference between Strava IDs and DB IDs

Full sync runs on first run (no `last_full_sync_timestamp`) and every `FULL_SYNC_INTERVAL_HOURS` (default: 1). The constant is defined at `sync.py:34`.

Both modes share the same `sync_activities()` function (`sync.py:407`) via the `full: bool` keyword argument.

## Lazy polyline re-decode

`upsert_activity()` (`sync.py:375`) only re-decodes and re-inserts track points when `map_summary_polyline` has changed. The existing polyline value is fetched from the DB before the update and compared.

**Why**: decoding and bulk-inserting hundreds of lat/lng points per activity on every incremental sync would be wasteful. Polylines almost never change after an activity is uploaded.

The decode itself (`sync.py:354`) is wrapped in a try/except — malformed polylines are logged as warnings and skipped without failing the sync.

## Upsert via existence check

There is no `INSERT OR REPLACE` or `ON CONFLICT` clause on activities. Instead, `upsert_activity()` explicitly checks for existence first (`sync.py:379`), then branches:

- Not found → `INSERT` with all columns
- Found → `UPDATE` all columns except `id`

**Why**: allows the lazy polyline re-decode logic — `INSERT OR REPLACE` would silently delete and re-insert the row, losing the ability to compare the old polyline value cleanly.

## Token rotation in DB, bootstrap from `.env`

`get_access_token()` (`sync.py:187`) follows a priority chain:

1. Check `config` table for a non-expired `access_token` → reuse it
2. Read `refresh_token` from `config` table
3. Fall back to `STRAVA_REFRESH_TOKEN` from `.env` if `config` has nothing (first run only)
4. Call Strava token endpoint, write new `access_token`, `refresh_token`, `expires_at` back to `config`

After the first successful token refresh, the `.env` `STRAVA_REFRESH_TOKEN` is superseded. The `config` table is the source of truth for tokens.

## `raw_json` forward compatibility column

Every activity row stores the full Strava API JSON in `raw_json` (`sync.py:349`). This allows querying fields that were not explicitly mapped to columns without requiring a schema migration or a re-sync from the API.

To access an unmapped field in SQLite: `json_extract(raw_json, '$.field_name')`.

## All paths relative to `__file__`

`BASE_DIR = Path(__file__).parent` (`sync.py:26`) anchors all file paths (DB, log, `.env`) to the script's own directory. This makes the cron entry `cd /path/to/strava && python sync.py` sufficient — no absolute paths need to be hardcoded inside the script.
