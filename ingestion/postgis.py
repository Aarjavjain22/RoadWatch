"""Synchronize committed local evidence into an initialized Buffalo PostGIS database."""

import json
from pathlib import Path
from uuid import uuid4

from ingestion.store import payloads, workspace


def wkt(point):
    return f"POINT({point[1]} {point[0]})" if point is not None else None


def sync(path, dsn):
    import psycopg
    from psycopg.types.json import Jsonb

    with workspace(path) as local:
        local.execute("CREATE TABLE IF NOT EXISTS workspace_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        local.execute("INSERT OR IGNORE INTO workspace_meta VALUES ('id', ?)", (uuid4().hex,))
        workspace_id = local.execute("SELECT value FROM workspace_meta WHERE key='id'").fetchone()[0]
        frames = {f["id"]: f for f in payloads(local, "frames")}
        pins = {pin["id"]: pin for pin in payloads(local, "hazards")}
        members = dict(local.execute("SELECT obs_id, hazard_id FROM hazard_members").fetchall())
        with psycopg.connect("" if dsn == "env" else dsn) as pg:
            pg.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("roadwatch-buffalo-sync",))
            pg.execute((Path(__file__).resolve().parents[1] / "db/buffalo_runtime.sql").read_text())
            with pg.cursor() as cursor:
                cursor.executemany("""
                    INSERT INTO drives (drive_id, vehicle, route, started_at, video_path, privacy_blurred)
                    VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (drive_id) DO UPDATE SET
                        vehicle=excluded.vehicle, route=excluded.route, started_at=excluded.started_at,
                        video_path=excluded.video_path, privacy_blurred=excluded.privacy_blurred
                """, ((d["id"], d["vehicle_id"], d["route"], d["started_at"], d["video_ref"], d["privacy_reviewed"])
                      for d in payloads(local, "drives")))
                cursor.executemany("""
                    INSERT INTO gps_points (drive_id, ts, geom, speed_kph, heading_deg, accuracy_m, matched_geom, osm_way_id)
                    VALUES (%s,%s,ST_GeomFromText(%s,4326),%s,%s,%s,ST_GeomFromText(%s,4326),
                            (SELECT osm_way_id FROM roads WHERE osm_way_id=%s))
                    ON CONFLICT (drive_id, ts) DO UPDATE SET geom=excluded.geom, speed_kph=excluded.speed_kph,
                        heading_deg=excluded.heading_deg, accuracy_m=excluded.accuracy_m,
                        matched_geom=excluded.matched_geom, osm_way_id=excluded.osm_way_id
                """, ((r[0], p["ts"], wkt(p["point"]), p["speed_kph"], p["heading_deg"], p["accuracy_m"],
                       wkt(p.get("matched_point")), p.get("osm_way_id"))
                      for r in local.execute("SELECT drive_id, payload FROM gps_points")
                      for p in (json.loads(r[1]),)))
                cursor.executemany("""
                    INSERT INTO frames (frame_id, drive_id, ts, image_path, geom, heading_deg, speed_kph,
                                        weather_hour, location_source, clock_known, offset_s, osm_way_id)
                    VALUES (%s,%s,%s,%s,ST_GeomFromText(%s,4326),%s,%s,
                            (SELECT observed_at FROM weather_hourly WHERE observed_at=%s),%s,%s,%s,
                            (SELECT osm_way_id FROM roads WHERE osm_way_id=%s))
                    ON CONFLICT (frame_id) DO UPDATE SET geom=excluded.geom, ts=excluded.ts,
                        image_path=excluded.image_path, heading_deg=excluded.heading_deg, speed_kph=excluded.speed_kph,
                        weather_hour=excluded.weather_hour, location_source=excluded.location_source,
                        clock_known=excluded.clock_known, offset_s=excluded.offset_s, osm_way_id=excluded.osm_way_id
                """, ((f["id"], f["drive_id"], f["ts"], f["image_ref"], wkt(f["point"]), f["heading_deg"],
                       f["speed_kph"], f["weather_hour"], f["location_source"], f["clock_known"], f["offset_s"],
                       f.get("osm_way_id")) for f in frames.values()))
                cursor.execute("""
                    DELETE FROM hazards WHERE hazard_id IN
                        (SELECT hazard_id FROM buffalo_workspace_hazards WHERE workspace_id=%s)
                        AND NOT (hazard_id = ANY(%s::text[]))
                """, (workspace_id, list(pins)))
                cursor.executemany("""
                    INSERT INTO hazards (hazard_id, class, state, geom, uncertainty_m, n_observations, n_vehicles,
                                         max_confidence, severity, first_seen, last_seen, osm_way_id, linked_311_case,
                                         buffalo_details)
                    VALUES (%s,%s,%s,ST_GeomFromText(%s,4326),%s,%s,%s,%s,%s,%s,%s,
                            (SELECT osm_way_id FROM roads WHERE osm_way_id=%s),
                            (SELECT case_number FROM city_311_road_requests WHERE case_number=%s),%s)
                    ON CONFLICT (hazard_id) DO UPDATE SET state=excluded.state, geom=excluded.geom,
                        uncertainty_m=excluded.uncertainty_m, n_observations=excluded.n_observations,
                        n_vehicles=excluded.n_vehicles, max_confidence=excluded.max_confidence,
                        severity=excluded.severity, first_seen=excluded.first_seen, last_seen=excluded.last_seen,
                        osm_way_id=excluded.osm_way_id, linked_311_case=excluded.linked_311_case,
                        buffalo_details=excluded.buffalo_details
                """, ((p["id"], p["hazard_type"], p["state"], wkt(p["point"]), p["uncertainty_m"], p["n_observations"],
                       p["n_vehicles"], p["max_confidence"], p["severity"], p["first_seen"], p["last_seen"],
                       p["osm_way_id"], p.get("linked_311_case"), Jsonb(p)) for p in pins.values()))
                cursor.executemany("""
                    INSERT INTO buffalo_workspace_hazards VALUES (%s,%s)
                    ON CONFLICT (hazard_id) DO NOTHING
                """, ((pin_id, workspace_id) for pin_id in pins))
                cursor.executemany("""
                    INSERT INTO detections (observation_id, frame_id, model_version, class, confidence,
                                            x1,y1,x2,y2,est_geom,uncertainty_m)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,ST_GeomFromText(%s,4326),%s)
                    ON CONFLICT (observation_id) DO UPDATE SET confidence=excluded.confidence,
                        x1=excluded.x1, y1=excluded.y1, x2=excluded.x2, y2=excluded.y2,
                        est_geom=excluded.est_geom, uncertainty_m=excluded.uncertainty_m
                """, ((o["id"], o["frame_id"], o["model_version"], o["hazard_type"], o["confidence"],
                       *(o["bbox"] or [None] * 4), wkt(o.get("point")), o.get("uncertainty_m"))
                      for o in payloads(local, "observations")))
                cursor.execute("""
                    DELETE FROM buffalo_hazard_members WHERE hazard_id IN
                      (SELECT hazard_id FROM buffalo_workspace_hazards WHERE workspace_id=%s)
                """, (workspace_id,))
                cursor.executemany("""
                    INSERT INTO buffalo_hazard_members VALUES (%s,%s)
                    ON CONFLICT (observation_id) DO UPDATE SET hazard_id=excluded.hazard_id
                """, members.items())
                cursor.execute("""
                    DELETE FROM buffalo_311_drafts WHERE hazard_id IN
                      (SELECT hazard_id FROM buffalo_workspace_hazards WHERE workspace_id=%s)
                """, (workspace_id,))
                cursor.executemany("""
                    INSERT INTO buffalo_311_drafts VALUES (%s,%s,%s)
                    ON CONFLICT (draft_id) DO UPDATE SET payload=excluded.payload
                """, ((d["id"], d["hazard_id"], Jsonb(d)) for d in payloads(local, "drafts")))
