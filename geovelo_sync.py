#!/usr/bin/env python3
"""
Strava → GeoVelo sync script.

Reads 'Ride' activities from the local SQLite database and uploads
them to GeoVelo as GPX files. Skips activities without GPS data and
activities already uploaded.

Can be run standalone:
    .venv/bin/python geovelo_sync.py

Runs on its own cron schedule, independently of sync.py.

Prerequisites:
    1. Run geovelo_init_db.py once to create the geovelo_uploads table.
    2. Fill in .env (see .env.example).
"""

import base64
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from html import escape as xml_escape
from pathlib import Path

import requests
from dotenv import load_dotenv

# ── Config ────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "strava.db"
LOG_PATH = BASE_DIR / "geovelo_sync.log"
ENV_PATH = BASE_DIR / ".env"

GEOVELO_AUTH_URL = "https://backend.geovelo.fr/api/v1/authentication/geovelo"
GEOVELO_UPLOAD_URL = "https://backend.geovelo.fr/api/v2/user_trace_from_gpx"
DEFAULT_API_KEY = "0f8c781a-b4b4-4d19-b931-1e82f22e769f"
DEFAULT_SOURCE = "website"

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

GEOVELO_TABLE = """
CREATE TABLE IF NOT EXISTS geovelo_uploads (
    activity_id  INTEGER PRIMARY KEY,
    uploaded_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status       TEXT NOT NULL,
    error_msg    TEXT,
    FOREIGN KEY (activity_id) REFERENCES activities(id)
);
"""


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def ensure_table(conn: sqlite3.Connection) -> None:
    """Create geovelo_uploads if it doesn't exist yet (idempotent)."""
    conn.executescript(GEOVELO_TABLE)


def get_pending_activities(conn: sqlite3.Connection) -> list:
    """Return Ride activities not yet in geovelo_uploads."""
    return conn.execute(
        """
        SELECT a.id, a.name, a.start_date, a.elapsed_time
        FROM activities a
        WHERE (a.sport_type = 'Ride' OR a.type = 'Ride')
          AND a.is_deleted = 0
          AND a.id NOT IN (SELECT activity_id FROM geovelo_uploads)
        ORDER BY a.start_date ASC
        """
    ).fetchall()


def get_track_points(conn: sqlite3.Connection, activity_id: int) -> list:
    """Return ordered GPS points for an activity."""
    return conn.execute(
        """
        SELECT lat, lng FROM activity_tracks
        WHERE activity_id = ?
        ORDER BY point_order ASC
        """,
        (activity_id,),
    ).fetchall()


def record_upload(
    conn: sqlite3.Connection,
    activity_id: int,
    status: str,
    error_msg: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO geovelo_uploads (activity_id, uploaded_at, status, error_msg)
        VALUES (?, ?, ?, ?)
        """,
        (activity_id, datetime.now(timezone.utc).isoformat(), status, error_msg),
    )
    conn.commit()


# ── GPX builder ───────────────────────────────────────────────────────────────

def build_gpx(name: str, start_date: str, points: list, elapsed_time: int = 0) -> str:
    """Build a GPX string from a list of (lat, lng) tuples.

    Timestamps are distributed evenly over elapsed_time so that each trkpt
    carries a <time> element — required by GeoVelo's upload endpoint.
    """
    try:
        start_dt = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        start_dt = datetime.now(timezone.utc)

    n = len(points)
    trkpt_lines = []
    for i, (lat, lng) in enumerate(points):
        if elapsed_time > 0 and n > 1:
            offset = timedelta(seconds=elapsed_time * i / (n - 1))
        else:
            offset = timedelta(seconds=i)
        t = (start_dt + offset).strftime("%Y-%m-%dT%H:%M:%SZ")
        trkpt_lines.append(
            f'      <trkpt lat="{lat}" lon="{lng}"><time>{t}</time></trkpt>'
        )

    trkpts = "\n".join(trkpt_lines)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<gpx version="1.1" creator="strava-geovelo-sync"'
        ' xmlns="http://www.topografix.com/GPX/1/1">\n'
        f"  <metadata><time>{start_date}</time></metadata>\n"
        "  <trk>\n"
        f"    <name>{xml_escape(name)}</name>\n"
        "    <type>cycling</type>\n"
        "    <trkseg>\n"
        f"{trkpts}\n"
        "    </trkseg>\n"
        "  </trk>\n"
        "</gpx>"
    )


# ── GeoVelo API ───────────────────────────────────────────────────────────────

def load_geovelo_config() -> tuple[str, str, str]:
    """Load and validate GeoVelo credentials from environment."""
    load_dotenv(ENV_PATH)
    auth = os.getenv("GEOVELO_AUTHENTIFICATION", "").strip()
    api_key = os.getenv("GEOVELO_API_KEY", DEFAULT_API_KEY).strip()
    source = os.getenv("GEOVELO_SOURCE", DEFAULT_SOURCE).strip()

    if not auth:
        raise ValueError(
            "GEOVELO_AUTHENTIFICATION is not set in .env.\n"
            "Generate it with:\n"
            "  python -c \"import base64; "
            "print(base64.b64encode(b'email@example.com;yourpassword').decode())\""
        )
    return auth, api_key, source


def geovelo_authenticate(auth: str, api_key: str, source: str) -> str:
    """Authenticate with GeoVelo and return the session Authorization token."""
    resp = requests.post(
        GEOVELO_AUTH_URL,
        data="",
        headers={
            "Api-key": api_key,
            "Source": source,
            "Authentication": auth,
        },
        timeout=30,
    )
    resp.raise_for_status()
    token = resp.headers.get("authorization")
    if not token:
        raise ValueError(
            "GeoVelo authentication succeeded but returned no authorization token. "
            f"Response headers: {dict(resp.headers)}"
        )
    return token


def upload_gpx(
    token: str,
    api_key: str,
    source: str,
    gpx_content: str,
    name: str,
    debug: bool = False,
) -> None:
    """Upload a GPX file to GeoVelo."""
    headers = {
        "Api-key": api_key,
        "Source": source,
        "Authorization": token,
    }
    files = {"gpx": (f"{name}.gpx", gpx_content.encode("utf-8"))}
    data = {"title": name}

    if debug:
        gpx_path = BASE_DIR / "debug_activity.gpx"
        gpx_path.write_text(gpx_content, encoding="utf-8")
        log.debug("--- GPX saved to %s ---", gpx_path)
        log.debug("--- REQUEST HEADERS ---\n%s", headers)
        log.debug(
            "--- CURL EQUIVALENT ---\n"
            "curl -X POST '%s' \\\n"
            "  -H 'Api-key: %s' \\\n"
            "  -H 'Source: %s' \\\n"
            "  -H 'Authorization: %s' \\\n"
            "  -F 'gpx=@%s' \\\n"
            "  -F 'title=%s'",
            GEOVELO_UPLOAD_URL, api_key, source, token, gpx_path, name,
        )

    resp = requests.post(
        GEOVELO_UPLOAD_URL,
        files=files,
        data=data,
        headers=headers,
        timeout=60,
    )

    if debug:
        log.debug("--- RESPONSE STATUS: %s ---", resp.status_code)
        log.debug("--- RESPONSE HEADERS ---\n%s", dict(resp.headers))
        log.debug("--- RESPONSE BODY ---\n%s", resp.text)

    if not resp.ok:
        raise requests.HTTPError(
            f"{resp.status_code} {resp.reason} — response body: {resp.text!r}",
            response=resp,
        )


# ── Main sync logic ───────────────────────────────────────────────────────────

def sync_to_geovelo(debug: bool = False) -> None:
    """Upload pending Strava Ride activities to GeoVelo."""
    if debug:
        log.setLevel(logging.DEBUG)
        for h in log.handlers:
            h.setLevel(logging.DEBUG)

    if not DB_PATH.exists():
        log.error("Database not found at %s — run sync.py first.", DB_PATH)
        return

    auth, api_key, source = load_geovelo_config()

    with get_db() as conn:
        ensure_table(conn)

        activities = get_pending_activities(conn)
        limit = 1 if debug else len(activities)
        log.info("GeoVelo sync: %d Ride activit%s to process%s.",
                 len(activities), "y" if len(activities) == 1 else "ies",
                 " (debug: processing first only)" if debug else "")

        if not activities:
            return

        try:
            token = geovelo_authenticate(auth, api_key, source)
            log.info("Authenticated with GeoVelo. Token prefix: %s", token[:20])
        except Exception as exc:
            log.error("GeoVelo authentication failed: %s", exc)
            return

        success = skipped = errors = 0

        for row in activities[:limit]:
            activity_id = row["id"]
            name = row["name"] or f"Activity {activity_id}"
            start_date = row["start_date"] or "1970-01-01T00:00:00Z"
            elapsed_time = row["elapsed_time"] or 0

            points = get_track_points(conn, activity_id)

            if not points:
                log.info(
                    "Skipped activity %d '%s': no GPS track.", activity_id, name
                )
                if not debug:
                    record_upload(conn, activity_id, "skipped")
                skipped += 1
                continue

            try:
                gpx = build_gpx(name, start_date, [(p["lat"], p["lng"]) for p in points], elapsed_time)
                upload_gpx(token, api_key, source, gpx, name, debug=debug)
                if not debug:
                    record_upload(conn, activity_id, "success")
                log.info(
                    "Uploaded activity %d '%s' (%d points).",
                    activity_id, name, len(points),
                )
                success += 1
            except Exception as exc:
                if not debug:
                    record_upload(conn, activity_id, "error", str(exc))
                log.warning(
                    "Failed to upload activity %d '%s': %s", activity_id, name, exc
                )
                errors += 1

    log.info(
        "GeoVelo sync done: %d uploaded, %d skipped (no GPS), %d errors.",
        success, skipped, errors,
    )


if __name__ == "__main__":
    import sys
    debug_mode = "--debug" in sys.argv
    sync_to_geovelo(debug=debug_mode)
