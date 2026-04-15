# CLAUDE.md

## Purpose

Two independent Python tools sharing a local SQLite database:
- **Strava → Grafana**: syncs Strava activity history to SQLite, served to a Grafana dashboard
- **Strava → GeoVelo**: auto-uploads `Ride` activities as GPX to the user's GeoVelo account

Both run unattended on separate cron schedules.

## Tech stack

- **Python 3** — standalone scripts, no framework
- **SQLite** (WAL mode) — shared local storage, read directly by Grafana and geovelo_sync
- **Strava API v3** — `GET /athlete/activities`, OAuth 2.0 with refresh token rotation
- **GeoVelo API** — token auth via `POST /api/v1/authentication/geovelo`, upload via `POST /api/v2/user_trace_from_gpx`
- **Grafana** — runs in Docker on port 3000, uses `frser-sqlite-datasource` plugin
- **Libraries** — `requests`, `python-dotenv`, `polyline`

## Key files

| File | Purpose |
|---|---|
| `sync.py` | Strava → SQLite cron entry point. Auth, API fetch, upsert, polyline decode, sync logging |
| `auth.py` | One-time interactive OAuth flow to obtain the initial Strava refresh token |
| `geovelo_sync.py` | SQLite → GeoVelo cron entry point. Reads Ride activities, builds GPX, uploads |
| `geovelo_init_db.py` | One-time migration: adds `geovelo_uploads` table to existing `strava.db` |
| `strava.db` | Auto-created SQLite database (not committed) |
| `.env` | All runtime credentials — Strava + GeoVelo (not committed) — see `.env.example` |
| `README.md` | Full operational documentation: setup for both features, SQL queries, Grafana config |

## Essential commands

```bash
# Install dependencies (Debian/Raspberry Pi — requires virtualenv)
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Get initial Strava OAuth refresh token (run once)
.venv/bin/python auth.py

# Run Strava sync manually
.venv/bin/python sync.py

# Enable GeoVelo feature: create the uploads tracking table (run once)
.venv/bin/python geovelo_init_db.py

# Run GeoVelo sync manually
.venv/bin/python geovelo_sync.py

# Cron entries
*/10 * * * * cd /path/to/repo && .venv/bin/python sync.py
*/5  * * * * cd /path/to/repo && .venv/bin/python geovelo_sync.py
```

## Database tables

- `activities` — one row per Strava activity, all summary fields (`sync.py:51`)
- `activity_tracks` — decoded GPS points, one row per lat/lng (`sync.py:109`)
- `sync_log` — one row per Strava sync run with outcome (`sync.py:118`)
- `config` — key/value runtime state: tokens, timestamps (`sync.py:130`)
- `geovelo_uploads` — one row per activity upload attempt: status, error, timestamp (added by `geovelo_init_db.py`)

## Sync behaviour at a glance

### Strava → SQLite (`sync.py`)
- **Always full sync**: fetches all activities every run (paginated, 200/page), upserts, detects deletions
- Deleted activities are flagged `is_deleted = 1`, never physically removed

### SQLite → GeoVelo (`geovelo_sync.py`)
- Queries `Ride` activities not yet in `geovelo_uploads`
- Skips activities with no GPS track (indoor/manual) — logged as `skipped`
- Builds a minimal GPX with synthetic timestamps (distributed evenly over `elapsed_time`)
- Authenticates with GeoVelo once per run, then uploads each activity
- Records every attempt in `geovelo_uploads` with status `success`, `skipped`, or `error`
- `--debug` flag: processes only the first pending activity, saves GPX to `debug_activity.gpx`, does not write to DB

## Additional documentation

| File | When to check |
|---|---|
| `.claude/docs/architectural_patterns.md` | Before modifying sync logic, adding fields, or extending the DB schema |
| `README.md` | Full setup for both features, Grafana datasource config, docker-compose, SQL queries |
