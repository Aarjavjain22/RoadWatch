"""Local Buffalo workspace and read-only public snapshot access."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from ingestion.geo import Roads


@contextmanager
def workspace(path):
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys = ON")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS drives (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS gps_points (
                drive_id TEXT REFERENCES drives(id), offset_s REAL, payload TEXT NOT NULL,
                PRIMARY KEY (drive_id, offset_s));
            CREATE TABLE IF NOT EXISTS frames (
                id TEXT PRIMARY KEY, drive_id TEXT NOT NULL REFERENCES drives(id), payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS observations (
                id TEXT PRIMARY KEY, frame_id TEXT NOT NULL REFERENCES frames(id), payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS frame_results (
                frame_id TEXT PRIMARY KEY REFERENCES frames(id), payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS hazards (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS drafts (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS hazard_members (
                obs_id TEXT PRIMARY KEY REFERENCES observations(id), hazard_id TEXT NOT NULL REFERENCES hazards(id));
        """)
        with db:
            db.execute("BEGIN IMMEDIATE")
            yield db
    finally:
        db.close()


@contextmanager
def baseline(path):
    if path is None:
        yield None
        return
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


def road_rows(db):
    if db is not None:
        for row in db.execute("SELECT osm_way_id, name, geometry_geojson FROM roads"):
            yield row[0], row[1], json.loads(row[2])["coordinates"]


def roads(db):
    return Roads(road_rows(db))


def payloads(db, table):
    return [json.loads(row[0]) for row in db.execute(f"SELECT payload FROM {table} ORDER BY 1")]


def put(db, table, ids, payload):
    fields = ", ".join((*ids, "payload"))
    placeholders = ", ".join("?" for _ in range(len(ids) + 1))
    db.execute(
        f"INSERT INTO {table} ({fields}) VALUES ({placeholders}) "
        "ON CONFLICT DO UPDATE SET payload=excluded.payload",
        (*ids.values(), json.dumps(payload, allow_nan=False)),
    )
