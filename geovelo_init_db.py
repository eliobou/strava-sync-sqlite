#!/usr/bin/env python3
"""
One-time migration: adds the geovelo_uploads table to strava.db.
Run this once before using geovelo_sync.py.

    .venv/bin/python geovelo_init_db.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "strava.db"

MIGRATION = """
CREATE TABLE IF NOT EXISTS geovelo_uploads (
    activity_id  INTEGER PRIMARY KEY,
    uploaded_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status       TEXT NOT NULL,   -- 'success' | 'skipped' | 'error'
    error_msg    TEXT,
    FOREIGN KEY (activity_id) REFERENCES activities(id)
);
"""


def main() -> None:
    if not DB_PATH.exists():
        print(f"ERROR: database not found at {DB_PATH}")
        print("Run sync.py at least once first to create the database.")
        raise SystemExit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.executescript(MIGRATION)
    conn.commit()
    conn.close()
    print(f"Migration complete: geovelo_uploads table ready in {DB_PATH}")


if __name__ == "__main__":
    main()
