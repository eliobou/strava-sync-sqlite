# CLAUDE.md

## Purpose

Syncs Strava activity history to a local SQLite database and serves it to a Grafana dashboard via the SQLite datasource plugin. Designed to run unattended on a cron schedule.

## Tech stack

- **Python 3** — single-file ETL script, no framework
- **SQLite** (WAL mode) — local storage, read directly by Grafana
- **Strava API v3** — `GET /athlete/activities`, OAuth 2.0 with refresh token rotation
- **Grafana** — runs in Docker on port 3000, uses `frser-sqlite-datasource` plugin
- **Libraries** — `requests`, `python-dotenv`, `polyline`

## Key files

| File | Purpose |
|---|---|
| `sync.py` | Cron entry point. Auth, API fetch, upsert, polyline decode, sync logging |
| `auth.py` | One-time interactive OAuth flow to obtain the initial refresh token |
| `strava.db` | Auto-created SQLite database (not committed) |
| `.env` | Runtime credentials (not committed) — see `.env.example` |
| `README.md` | Full operational documentation: setup, SQL queries, Grafana config |

## Essential commands

```bash
# Install dependencies (Debian/Raspberry Pi — requires virtualenv)
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Get initial OAuth refresh token (run once)
.venv/bin/python auth.py

# Run a sync manually
.venv/bin/python sync.py

# Cron entry (every 10 minutes)
*/10 * * * * cd /path/to/strava-grafana-dashboard && .venv/bin/python sync.py
```

## Database tables

- `activities` — one row per Strava activity, all summary fields (`sync.py:51`)
- `activity_tracks` — decoded GPS points, one row per lat/lng (`sync.py:109`)
- `sync_log` — one row per sync run with outcome (`sync.py:118`)
- `config` — key/value runtime state: tokens, timestamps (`sync.py:130`)

## Sync behaviour at a glance

- **Incremental** (default): fetches only activities updated since `last_sync_timestamp` — 1 API call
- **Full** (every 1h or first run): fetches all pages, detects deletions — ~3 API calls
- Trigger logic: `sync.py:462` (`needs_full_sync`)
- Deleted activities are flagged `is_deleted = 1`, never physically removed

## Additional documentation

| File | When to check |
|---|---|
| `.claude/docs/architectural_patterns.md` | Before modifying sync logic, adding fields, or extending the DB schema |
| `README.md` | Grafana datasource setup, docker-compose volume mount, example SQL queries |
