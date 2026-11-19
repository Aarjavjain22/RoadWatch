"""SQLite persistence for replay analyses, hazards, evidence, reports and settings."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager

from app.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    filename      TEXT UNIQUE NOT NULL,
    original_name TEXT,
    width         INTEGER,
    height        INTEGER,
    fps           REAL,
    duration      REAL,
    status        TEXT NOT NULL DEFAULT 'pending',
    progress      REAL NOT NULL DEFAULT 0,
    model         TEXT,
    error         TEXT,
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    analyzed_at   TEXT,
    source_path   TEXT
);
CREATE TABLE IF NOT EXISTS detections (
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    t        REAL NOT NULL,
    boxes    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_detections_video ON detections (video_id, t);
CREATE TABLE IF NOT EXISTS hazards (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id              INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    track_id              INTEGER NOT NULL,
    damage_type           TEXT NOT NULL,
    best_conf             REAL NOT NULL,
    first_t               REAL NOT NULL,
    last_t                REAL NOT NULL,
    observations          INTEGER NOT NULL,
    distance_m            REAL,
    visibility_condition  TEXT,
    ml_weather            TEXT,
    ml_weather_conf       REAL
);
CREATE TABLE IF NOT EXISTS evidence (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    hazard_id INTEGER NOT NULL REFERENCES hazards(id) ON DELETE CASCADE,
    path      TEXT NOT NULL,
    t         REAL NOT NULL,
    conf      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    damage_type TEXT NOT NULL,
    severity    TEXT NOT NULL,
    location    TEXT,
    lat         REAL,
    lon         REAL,
    notes       TEXT,
    photo       TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_CALIBRATION = {"camera_height_m": 1.4, "vfov_deg": 50.0, "horizon_frac": 0.45}

_lock = threading.Lock()


@contextmanager
def db():
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)
        try:
            conn.execute("ALTER TABLE videos ADD COLUMN source_path TEXT")
        except sqlite3.OperationalError:
            pass
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_videos_source_path "
            "ON videos (source_path) WHERE source_path IS NOT NULL"
        )
        # Interrupted analyses can't resume; surface them as failed so they can be re-run.
        conn.execute(
            "UPDATE videos SET status='failed', error='interrupted (server restarted)' "
            "WHERE status IN ('queued', 'analyzing')"
        )


def get_setting(key: str, default=None):
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_setting(key: str, value) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )


def get_calibration() -> dict:
    return {**DEFAULT_CALIBRATION, **(get_setting("calibration") or {})}


def rows(query: str, params: tuple = ()) -> list[dict]:
    with db() as conn:
        return [dict(r) for r in conn.execute(query, params).fetchall()]


def one(query: str, params: tuple = ()) -> dict | None:
    with db() as conn:
        r = conn.execute(query, params).fetchone()
    return dict(r) if r else None


def execute(query: str, params: tuple = ()) -> int:
    with db() as conn:
        return conn.execute(query, params).lastrowid
