# Strava → SQLite → Grafana

Syncs all Strava activities to a local SQLite database and exposes the data to a Grafana dashboard via the SQLite datasource plugin.

## Overview

`sync.py` runs on a cron schedule and pulls activity data from the Strava API v3. It stores every field available from the activity summary endpoint, decodes GPS polylines into individual lat/lng points, and handles updates and deletions. Grafana reads directly from the SQLite file.

---

## Project structure

```
strava/
├── sync.py          # Main sync script (cron entry point)
├── auth.py          # One-time OAuth helper to get the initial refresh token
├── requirements.txt
├── .env             # Credentials (not committed)
├── .env.example     # Template
├── strava.db        # SQLite database (auto-created on first run)
└── sync.log         # Appended by every run
```

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

Dependencies: `requests`, `python-dotenv`, `polyline`

### 2. Create `.env`

```bash
cp .env.example .env
```

Fill in `STRAVA_CLIENT_ID` and `STRAVA_CLIENT_SECRET` from [strava.com/settings/api](https://www.strava.com/settings/api).

### 3. Get the refresh token (once)

```bash
python auth.py
```

The script prints an authorization URL. Open it, authorize the app, paste the redirect URL back. Copy the printed `STRAVA_REFRESH_TOKEN` into `.env`.

Required OAuth scope: `read_all,activity:read_all`

After the first successful `sync.py` run, the token is stored and rotated automatically in the SQLite `config` table. The `.env` value is only used as a bootstrap fallback if the DB has no token yet.

### 4. First run

```bash
python sync.py
```

On first run: no `last_full_sync_timestamp` in the DB → triggers a full sync fetching all historical activities.

### 5. Cron (Debian)

```bash
crontab -e
```

```
*/10 * * * * cd /path/to/strava && /usr/bin/python3 sync.py
```

Redirect stdout/stderr to the log if you want cron to stay silent:

```
*/10 * * * * cd /path/to/strava && /usr/bin/python3 sync.py >> sync.log 2>&1
```

---

## Sync logic

### Two sync modes

| Mode | Trigger | API calls | What it does |
|---|---|---|---|
| **Full** | First run ever, or `>= 24h` since last full | 1 per 200 activities (paginated) | Fetches all activities, upserts everything, detects deletions |
| **Incremental** | Every other run | 1 (usually) | Fetches only activities updated since `last_sync_timestamp` |

The decision is made at startup by checking `last_full_sync_timestamp` in the `config` table. If it is absent or older than `FULL_SYNC_INTERVAL_HOURS` (default: 1), a full sync runs.

### Incremental sync detail

Uses the Strava API `after` parameter (Unix timestamp) on `GET /athlete/activities`. Only activities created or updated after the last sync timestamp are returned. This is typically 0–few activities per run → 1 API call.

### Full sync and deletion detection

Fetches all pages (200 per page). Builds the set of all Strava activity IDs. Compares with all non-deleted IDs in the DB. Any ID present in the DB but absent from Strava is marked `is_deleted = 1`. Activities are never physically deleted from the DB.

### Upsert logic

For each fetched activity:
- If the ID is not in the DB → `INSERT` + decode and store track points
- If the ID exists → `UPDATE` all fields; re-decode track points only if `map_summary_polyline` changed

### Token rotation

Strava rotates the refresh token on every use. The new `access_token`, `refresh_token`, and `expires_at` are always written back to the `config` table immediately after refresh. The access token is reused across runs until it expires (with a 60-second safety margin).

### Rate limits

Strava enforces **100 requests / 15 min** and **1 000 requests / day**.

Estimated usage with this setup:
- 10-min cron = 144 runs/day
- Incremental runs: 1 call each → 138 calls/day
- 24 full syncs/day (every hour): ~3 calls each → ~72 calls/day
- **Total: ~210 calls/day** — well within the 1 000/day limit

On HTTP 429, the script reads the `X-RateLimit-Reset` header and sleeps until the window resets before retrying once.

---

## Polyline decoding

Strava returns a `summary_polyline` field (Google Encoded Polyline format) inside the `map` object of each activity. This is a compact string encoding of the GPS track.

`sync.py` decodes it using the `polyline` Python library (`polyline.decode()`), which returns a list of `(lat, lng)` tuples. Each tuple is stored as a row in the `activity_tracks` table with its sequential `point_order`.

This happens **without any extra API call** — the polyline is already included in the `/athlete/activities` response.

Indoor activities and manual entries without GPS have an empty or null polyline; the decode step is skipped and no rows are inserted for those activities.

---

## Database schema

SQLite file: `strava.db` (WAL mode, foreign keys enabled)

### `activities`

One row per Strava activity. All fields sourced from `GET /athlete/activities` (summary representation).

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | Strava activity ID |
| `athlete_id` | INTEGER | |
| `name` | TEXT | |
| `distance` | REAL | Meters |
| `moving_time` | INTEGER | Seconds |
| `elapsed_time` | INTEGER | Seconds |
| `total_elevation_gain` | REAL | Meters |
| `type` | TEXT | Deprecated Strava field, kept for compatibility |
| `sport_type` | TEXT | e.g. `Run`, `Ride`, `Swim`, `WeightTraining` |
| `workout_type` | INTEGER | |
| `start_date` | TEXT | ISO 8601, UTC |
| `start_date_local` | TEXT | ISO 8601, athlete's local time |
| `timezone` | TEXT | |
| `utc_offset` | REAL | |
| `start_lat` / `start_lng` | REAL | From `start_latlng` array |
| `end_lat` / `end_lng` | REAL | From `end_latlng` array |
| `achievement_count` | INTEGER | |
| `kudos_count` | INTEGER | |
| `comment_count` | INTEGER | |
| `athlete_count` | INTEGER | Number of athletes (for group activities) |
| `photo_count` | INTEGER | |
| `total_photo_count` | INTEGER | |
| `map_id` | TEXT | |
| `map_summary_polyline` | TEXT | Raw encoded polyline, kept for reference |
| `trainer` | INTEGER | Boolean (0/1) |
| `commute` | INTEGER | Boolean (0/1) |
| `manual` | INTEGER | Boolean (0/1) |
| `private` | INTEGER | Boolean (0/1) |
| `flagged` | INTEGER | Boolean (0/1) |
| `gear_id` | TEXT | |
| `average_speed` | REAL | m/s |
| `max_speed` | REAL | m/s |
| `average_cadence` | REAL | rpm |
| `average_watts` | REAL | |
| `weighted_average_watts` | INTEGER | |
| `kilojoules` | REAL | |
| `device_watts` | INTEGER | Boolean: power from device vs estimated |
| `has_heartrate` | INTEGER | Boolean |
| `average_heartrate` | REAL | bpm |
| `max_heartrate` | REAL | bpm |
| `max_watts` | INTEGER | |
| `elev_high` | REAL | Meters |
| `elev_low` | REAL | Meters |
| `pr_count` | INTEGER | |
| `has_kudoed` | INTEGER | Boolean |
| `suffer_score` | REAL | Strava relative effort |
| `visibility` | TEXT | `everyone`, `followers_only`, `only_me` |
| `upload_id` | INTEGER | |
| `external_id` | TEXT | ID from the original device/app |
| `is_deleted` | INTEGER | `1` if removed from Strava, `0` otherwise |
| `raw_json` | TEXT | Full JSON from API, for future use |
| `created_at` | TEXT | First time the row was inserted |
| `updated_at` | TEXT | Last time the row was modified by sync |

Indexes: `start_date`, `sport_type`, `is_deleted`

### `activity_tracks`

Decoded GPS points from `map_summary_polyline`. One row per point.

| Column | Type | Notes |
|---|---|---|
| `activity_id` | INTEGER | FK → `activities.id` |
| `point_order` | INTEGER | 0-based sequential index |
| `lat` | REAL | |
| `lng` | REAL | |

Primary key: `(activity_id, point_order)`

Index: `activity_id`

### `sync_log`

One row per sync run.

| Column | Notes |
|---|---|
| `sync_type` | `full` or `incremental` |
| `started_at` | ISO 8601 UTC |
| `completed_at` | ISO 8601 UTC |
| `activities_added` | |
| `activities_updated` | |
| `activities_deleted` | Flagged as deleted |
| `status` | `success` or `error` |
| `error_message` | Populated on error |

### `config`

Key/value store for runtime state.

| Key | Value |
|---|---|
| `access_token` | Current Strava access token |
| `refresh_token` | Current Strava refresh token (rotated after every use) |
| `token_expires_at` | Unix timestamp |
| `last_sync_timestamp` | Unix timestamp of last successful sync |
| `last_full_sync_timestamp` | Unix timestamp of last full sync |

---

## Grafana setup

### Docker volume

The Grafana container needs read access to `strava.db`. Mount it as a volume.

**docker-compose.yml:**

```yaml
services:
  grafana:
    image: grafana/grafana:latest
    container_name: grafana
    restart: unless-stopped
    ports:
      - "3030:3000"
    environment:
      - GF_SECURITY_ADMIN_USER=${GF_SECURITY_ADMIN_USER}
      - GF_SECURITY_ADMIN_PASSWORD=${GF_SECURITY_ADMIN_PASSWORD}
      - GF_INSTALL_PLUGINS=frser-sqlite-datasource
    volumes:
      # Persistent Grafana data (dashboards, users, config)
      - ./data:/var/lib/grafana
      # Mount your SQLite file (read-only)
      - /path/to/strava-grafana-dashboard/strava.db:/var/lib/grafana/strava.db:ro
```

If the plugin is already installed in your Grafana image, remove the `GF_INSTALL_PLUGINS` line.

### Datasource configuration

In Grafana → Connections → Data Sources → Add → SQLite:

- **Path**: `/var/lib/grafana/strava.db`

### Example queries

**All GPS track points (for Geomap / heatmap overlay)**

```sql
SELECT t.lat, t.lng
FROM activity_tracks t
JOIN activities a ON a.id = t.activity_id
WHERE a.is_deleted = 0
```

**All GPS track points for a specific sport**

```sql
SELECT t.lat, t.lng
FROM activity_tracks t
JOIN activities a ON a.id = t.activity_id
WHERE a.is_deleted = 0
  AND a.sport_type = 'Run'
```

**Monthly cumulative distance (km)**

```sql
SELECT
    strftime('%Y-%m', start_date_local) AS month,
    ROUND(SUM(distance) / 1000.0, 2)   AS distance_km
FROM activities
WHERE is_deleted = 0
GROUP BY month
ORDER BY month
```

**Distance per month, per sport type**

```sql
SELECT
    strftime('%Y-%m', start_date_local) AS month,
    sport_type,
    ROUND(SUM(distance) / 1000.0, 2)   AS distance_km
FROM activities
WHERE is_deleted = 0
GROUP BY month, sport_type
ORDER BY month
```

**Weekly volume (distance + elevation)**

```sql
SELECT
    strftime('%Y-W%W', start_date_local)        AS week,
    ROUND(SUM(distance) / 1000.0, 2)            AS distance_km,
    ROUND(SUM(total_elevation_gain), 0)         AS elevation_m,
    ROUND(SUM(moving_time) / 3600.0, 2)         AS moving_hours
FROM activities
WHERE is_deleted = 0
GROUP BY week
ORDER BY week
```

**Activity count per sport type**

```sql
SELECT sport_type, COUNT(*) AS count
FROM activities
WHERE is_deleted = 0
GROUP BY sport_type
ORDER BY count DESC
```

**Sync history (last 20 runs)**

```sql
SELECT sync_type, started_at, activities_added, activities_updated, activities_deleted, status
FROM sync_log
ORDER BY id DESC
LIMIT 20
```

### Geomap panel tip

In the Geomap panel, use **Table** query type. Set location mode to **Auto** with `lat` and `lng` as the coordinate columns. To overlay all tracks as a heatmap, use the Heatmap layer type with the all-points query above. Point density will naturally be higher on frequently ridden/run routes.

---

## Strava API reference

- Base URL: `https://www.strava.com/api/v3`
- Auth: `https://www.strava.com/oauth/token`
- Endpoint used: `GET /athlete/activities`
- Pagination: `per_page=200`, `page=N`
- Incremental filter: `after=<unix_timestamp>`
- Rate limits: 100 req / 15 min, 1 000 req / day
