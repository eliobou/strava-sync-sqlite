#!/usr/bin/env python3
"""
Strava → SQLite sync script.
Designed to run via cron every 15 minutes.

Strategy: always full sync (fetch all activities, detect deletions).
Strava API budget: ~3 requests per run × 96 runs/day = 288 requests/day (limit: 1000/day).
"""

import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import polyline as polyline_lib
import requests
from dotenv import load_dotenv

# ── Config ────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "strava.db"
LOG_PATH = BASE_DIR / "sync.log"
ENV_PATH = BASE_DIR / ".env"

STRAVA_API_BASE = "https://www.strava.com/api/v3"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
PER_PAGE = 200

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── Database ──────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
    id                      INTEGER PRIMARY KEY,
    athlete_id              INTEGER,
    name                    TEXT,
    distance                REAL,
    moving_time             INTEGER,
    elapsed_time            INTEGER,
    total_elevation_gain    REAL,
    type                    TEXT,
    sport_type              TEXT,
    workout_type            INTEGER,
    start_date              TEXT,
    start_date_local        TEXT,
    timezone                TEXT,
    utc_offset              REAL,
    start_lat               REAL,
    start_lng               REAL,
    end_lat                 REAL,
    end_lng                 REAL,
    achievement_count       INTEGER,
    kudos_count             INTEGER,
    comment_count           INTEGER,
    athlete_count           INTEGER,
    photo_count             INTEGER,
    total_photo_count       INTEGER,
    map_id                  TEXT,
    map_summary_polyline    TEXT,
    trainer                 INTEGER,
    commute                 INTEGER,
    manual                  INTEGER,
    private                 INTEGER,
    flagged                 INTEGER,
    gear_id                 TEXT,
    average_speed           REAL,
    max_speed               REAL,
    average_cadence         REAL,
    average_watts           REAL,
    weighted_average_watts  INTEGER,
    kilojoules              REAL,
    device_watts            INTEGER,
    has_heartrate           INTEGER,
    average_heartrate       REAL,
    max_heartrate           REAL,
    max_watts               INTEGER,
    elev_high               REAL,
    elev_low                REAL,
    pr_count                INTEGER,
    has_kudoed              INTEGER,
    suffer_score            REAL,
    visibility              TEXT,
    upload_id               INTEGER,
    external_id             TEXT,
    is_deleted              INTEGER DEFAULT 0,
    raw_json                TEXT,
    created_at              TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at              TEXT
);

CREATE TABLE IF NOT EXISTS activity_tracks (
    activity_id  INTEGER NOT NULL,
    point_order  INTEGER NOT NULL,
    lat          REAL NOT NULL,
    lng          REAL NOT NULL,
    PRIMARY KEY (activity_id, point_order),
    FOREIGN KEY (activity_id) REFERENCES activities(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS sync_log (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at           TEXT,
    completed_at         TEXT,
    activities_added     INTEGER DEFAULT 0,
    activities_updated   INTEGER DEFAULT 0,
    activities_deleted   INTEGER DEFAULT 0,
    status               TEXT,
    error_message        TEXT
);

CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_activities_start_date
    ON activities(start_date);
CREATE INDEX IF NOT EXISTS idx_activities_sport_type
    ON activities(sport_type);
CREATE INDEX IF NOT EXISTS idx_activities_is_deleted
    ON activities(is_deleted);
CREATE INDEX IF NOT EXISTS idx_activity_tracks_activity_id
    ON activity_tracks(activity_id);
"""


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    with get_db() as conn:
        conn.executescript(SCHEMA)
    log.info("Database ready at %s", DB_PATH)


def get_config(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_config(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
        (key, str(value)),
    )


# ── Auth ──────────────────────────────────────────────────────────────────────

def load_env_credentials():
    load_dotenv(ENV_PATH)
    client_id = os.getenv("STRAVA_CLIENT_ID")
    client_secret = os.getenv("STRAVA_CLIENT_SECRET")
    initial_refresh_token = os.getenv("STRAVA_REFRESH_TOKEN")

    if not client_id or not client_secret:
        raise ValueError("STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET must be set in .env")

    return client_id, client_secret, initial_refresh_token


def get_access_token(
    conn: sqlite3.Connection,
    client_id: str,
    client_secret: str,
    initial_refresh_token: str | None = None,
) -> str:
    """Return a valid access token, refreshing via OAuth if needed."""
    refresh_token = get_config(conn, "refresh_token") or initial_refresh_token

    if not refresh_token:
        raise ValueError(
            "No refresh token found. Run auth.py first, then set "
            "STRAVA_REFRESH_TOKEN in .env"
        )

    # Reuse cached token if still valid
    access_token = get_config(conn, "access_token")
    expires_at = get_config(conn, "token_expires_at")
    if access_token and expires_at and float(expires_at) > time.time() + 60:
        return access_token

    log.info("Refreshing Strava access token...")
    resp = requests.post(
        STRAVA_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    set_config(conn, "access_token", data["access_token"])
    set_config(conn, "refresh_token", data["refresh_token"])
    set_config(conn, "token_expires_at", data["expires_at"])
    conn.commit()

    log.info(
        "Token refreshed, expires at %s",
        datetime.fromtimestamp(data["expires_at"]).isoformat(),
    )
    return data["access_token"]


# ── Strava API client ─────────────────────────────────────────────────────────

class StravaClient:
    def __init__(self, access_token: str) -> None:
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {access_token}"})

    def _get(self, path: str, **params):
        url = f"{STRAVA_API_BASE}{path}"
        resp = self.session.get(url, params=params, timeout=30)

        if resp.status_code == 429:
            reset_ts = int(resp.headers.get("X-RateLimit-Reset", time.time() + 60))
            wait = max(reset_ts - int(time.time()), 1)
            log.warning("Rate limited — waiting %ds before retry", wait)
            time.sleep(wait)
            resp = self.session.get(url, params=params, timeout=30)

        resp.raise_for_status()

        usage = resp.headers.get("X-RateLimit-Usage", "?")
        limit = resp.headers.get("X-RateLimit-Limit", "?")
        log.debug("Rate limit usage: %s / %s", usage, limit)

        return resp.json()

    def get_all_activities(self) -> list[dict]:
        """Fetch all activities, paginating as needed."""
        activities = []
        page = 1
        while True:
            log.info("Fetching activities page %d...", page)
            batch = self._get("/athlete/activities", per_page=PER_PAGE, page=page)
            if not batch:
                break

            activities.extend(batch)
            log.info("  → %d activities fetched so far", len(activities))

            if len(batch) < PER_PAGE:
                break

            page += 1
            time.sleep(0.5)  # gentle throttle

        return activities


# ── Data mapping ──────────────────────────────────────────────────────────────

def activity_to_row(a: dict) -> dict:
    """Map a Strava activity dict to a flat DB row dict."""
    start = a.get("start_latlng") or []
    end = a.get("end_latlng") or []
    m = a.get("map") or {}

    return {
        "id": a["id"],
        "athlete_id": (a.get("athlete") or {}).get("id"),
        "name": a.get("name"),
        "distance": a.get("distance"),
        "moving_time": a.get("moving_time"),
        "elapsed_time": a.get("elapsed_time"),
        "total_elevation_gain": a.get("total_elevation_gain"),
        "type": a.get("type"),
        "sport_type": a.get("sport_type"),
        "workout_type": a.get("workout_type"),
        "start_date": a.get("start_date"),
        "start_date_local": a.get("start_date_local"),
        "timezone": a.get("timezone"),
        "utc_offset": a.get("utc_offset"),
        "start_lat": start[0] if len(start) > 0 else None,
        "start_lng": start[1] if len(start) > 1 else None,
        "end_lat": end[0] if len(end) > 0 else None,
        "end_lng": end[1] if len(end) > 1 else None,
        "achievement_count": a.get("achievement_count"),
        "kudos_count": a.get("kudos_count"),
        "comment_count": a.get("comment_count"),
        "athlete_count": a.get("athlete_count"),
        "photo_count": a.get("photo_count"),
        "total_photo_count": a.get("total_photo_count"),
        "map_id": m.get("id"),
        "map_summary_polyline": m.get("summary_polyline"),
        "trainer": int(bool(a.get("trainer"))),
        "commute": int(bool(a.get("commute"))),
        "manual": int(bool(a.get("manual"))),
        "private": int(bool(a.get("private"))),
        "flagged": int(bool(a.get("flagged"))),
        "gear_id": a.get("gear_id"),
        "average_speed": a.get("average_speed"),
        "max_speed": a.get("max_speed"),
        "average_cadence": a.get("average_cadence"),
        "average_watts": a.get("average_watts"),
        "weighted_average_watts": a.get("weighted_average_watts"),
        "kilojoules": a.get("kilojoules"),
        "device_watts": int(bool(a.get("device_watts"))),
        "has_heartrate": int(bool(a.get("has_heartrate"))),
        "average_heartrate": a.get("average_heartrate"),
        "max_heartrate": a.get("max_heartrate"),
        "max_watts": a.get("max_watts"),
        "elev_high": a.get("elev_high"),
        "elev_low": a.get("elev_low"),
        "pr_count": a.get("pr_count"),
        "has_kudoed": int(bool(a.get("has_kudoed"))),
        "suffer_score": a.get("suffer_score"),
        "visibility": a.get("visibility"),
        "upload_id": a.get("upload_id"),
        "external_id": a.get("external_id"),
        "is_deleted": 0,
        "raw_json": json.dumps(a),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def decode_polyline(encoded: str | None) -> list[tuple[float, float]]:
    if not encoded:
        return []
    try:
        return polyline_lib.decode(encoded)
    except Exception as exc:
        log.warning("Could not decode polyline: %s", exc)
        return []


def save_track_points(conn: sqlite3.Connection, activity_id: int, encoded: str | None) -> None:
    conn.execute("DELETE FROM activity_tracks WHERE activity_id = ?", (activity_id,))
    points = decode_polyline(encoded)
    if points:
        conn.executemany(
            "INSERT INTO activity_tracks (activity_id, point_order, lat, lng) "
            "VALUES (?, ?, ?, ?)",
            [(activity_id, i, lat, lng) for i, (lat, lng) in enumerate(points)],
        )


# Fields that can change after an activity is created (social counters, user edits, etc.)
_MUTABLE_FIELDS = (
    "name", "kudos_count", "comment_count", "suffer_score",
    "visibility", "has_kudoed", "achievement_count",
    "photo_count", "total_photo_count", "map_summary_polyline",
)


def upsert_activity(conn: sqlite3.Connection, activity_data: dict) -> str:
    """Upsert one activity. Returns 'added', 'updated', or 'unchanged'."""
    row = activity_to_row(activity_data)

    existing = conn.execute(
        f"SELECT {', '.join(_MUTABLE_FIELDS)} FROM activities WHERE id = ?",
        (row["id"],),
    ).fetchone()

    if existing is None:
        cols = ", ".join(row.keys())
        placeholders = ", ".join("?" for _ in row)
        conn.execute(
            f"INSERT INTO activities ({cols}) VALUES ({placeholders})",
            list(row.values()),
        )
        save_track_points(conn, row["id"], row["map_summary_polyline"])
        return "added"

    if all(existing[f] == row[f] for f in _MUTABLE_FIELDS):
        return "unchanged"

    set_clause = ", ".join(f"{k} = ?" for k in row if k != "id")
    values = [v for k, v in row.items() if k != "id"] + [row["id"]]
    conn.execute(f"UPDATE activities SET {set_clause} WHERE id = ?", values)

    if existing["map_summary_polyline"] != row["map_summary_polyline"]:
        save_track_points(conn, row["id"], row["map_summary_polyline"])

    return "updated"


# ── Sync ──────────────────────────────────────────────────────────────────────

def sync(conn: sqlite3.Connection, client: StravaClient) -> tuple[int, int, int]:
    """
    Fetch all activities from Strava and upsert into the database.
    Detects deletions by comparing Strava IDs with the local database.
    Returns (added, updated, deleted).
    """
    log.info("Fetching all activities from Strava...")
    activities = client.get_all_activities()
    log.info("Processing %d activities...", len(activities))

    added = updated = 0
    for a in activities:
        result = upsert_activity(conn, a)
        if result == "added":
            added += 1
        elif result == "updated":
            updated += 1

    # Detect deletions: any local activity not returned by Strava is gone
    strava_ids = {a["id"] for a in activities}
    db_ids = {
        row[0]
        for row in conn.execute(
            "SELECT id FROM activities WHERE is_deleted = 0"
        ).fetchall()
    }
    gone = db_ids - strava_ids
    if gone:
        now = datetime.now(timezone.utc).isoformat()
        conn.executemany(
            "UPDATE activities SET is_deleted = 1, updated_at = ? WHERE id = ?",
            [(now, aid) for aid in gone],
        )
        log.info("Marked %d activities as deleted", len(gone))

    return added, updated, len(gone)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    init_db()

    client_id, client_secret, initial_refresh_token = load_env_credentials()
    started_at = datetime.now(timezone.utc).isoformat()

    log.info("=== Starting sync ===")

    with get_db() as conn:
        try:
            access_token = get_access_token(conn, client_id, client_secret, initial_refresh_token)
            client = StravaClient(access_token)

            added, updated, deleted = sync(conn, client)

            conn.execute(
                """INSERT INTO sync_log
                   (started_at, completed_at,
                    activities_added, activities_updated, activities_deleted, status)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    started_at,
                    datetime.now(timezone.utc).isoformat(),
                    added, updated, deleted,
                    "success",
                ),
            )
            conn.commit()
            log.info(
                "=== Sync done: +%d added, ~%d updated, -%d deleted ===",
                added, updated, deleted,
            )

        except Exception as exc:
            conn.rollback()
            conn.execute(
                """INSERT INTO sync_log
                   (started_at, completed_at, status, error_message)
                   VALUES (?, ?, ?, ?)""",
                (
                    started_at,
                    datetime.now(timezone.utc).isoformat(),
                    "error",
                    str(exc),
                ),
            )
            conn.commit()
            log.error("Sync failed: %s", exc, exc_info=True)
            sys.exit(1)


if __name__ == "__main__":
    main()
