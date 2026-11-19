"""Register extracted frames and an optional GPS track in a local Buffalo workspace."""

import argparse
import csv
import json
import math
import re
from bisect import bisect_left
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

from ingestion.geo import distance, heading, interpolate, point_on_route
from ingestion.store import baseline, put, roads, workspace

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def parse_time(text):
    value = datetime.fromisoformat(text)
    if value.utcoffset() is None:
        raise ValueError(f"Timestamp needs a UTC offset: {text}")
    return value.astimezone(timezone.utc)


def coordinates(lat, lon):
    result = float(lat), float(lon)
    if not (-90 <= result[0] <= 90 and -180 <= result[1] <= 180):
        raise ValueError(f"Invalid coordinate: {result}")
    return result


def timed_items(items, start):
    return [dict(item, offset_s=float(item["offset_s"]) if item.get("offset_s") not in (None, "")
                 else (parse_time(item["ts"]) - start).total_seconds()) for item in items]


def gps_estimate(points, offsets, offset_s, gap):
    i = bisect_left(offsets, offset_s)
    if i < len(points) and offsets[i] == offset_s:
        p = points[i]
        neighbor = points[i + 1] if i + 1 < len(points) else (points[i - 1] if i else None)
        bearing = p.get("heading_deg")
        if bearing is None and neighbor and abs(neighbor["offset_s"] - offset_s) <= gap and distance(p["point"], neighbor["point"]) >= 1:
            a, b = sorted((p, neighbor), key=lambda row: row["offset_s"])
            bearing = heading(a["point"], b["point"])
        return p["point"], bearing, p.get("speed_kph"), p.get("accuracy_m"), "gps"
    if not (0 < i < len(points)) or offsets[i] - offsets[i - 1] > gap:
        return None
    a, b = points[i - 1], points[i]
    elapsed = offsets[i] - offsets[i - 1]
    fraction = (offset_s - offsets[i - 1]) / elapsed
    moved = distance(a["point"], b["point"])
    bearing = heading(a["point"], b["point"]) if moved >= 1 else None
    if a.get("heading_deg") is not None and b.get("heading_deg") is not None:
        delta = (b["heading_deg"] - a["heading_deg"] + 180) % 360 - 180
        bearing = (a["heading_deg"] + fraction * delta) % 360
    accuracy = max((p["accuracy_m"] for p in (a, b) if p.get("accuracy_m") is not None), default=None)
    return interpolate(a["point"], b["point"], fraction), bearing, moved / elapsed * 3.6, accuracy, "interpolated"


def attach_road(row, road_index):
    if row["point"] is not None:
        match = road_index.snap(row["point"])
        if match:
            row.update(matched_point=match[0], osm_way_id=match[1], street=match[2])
    return row


def ingest(db, spec, image_root, road_index, public=None):
    drive_id = spec["drive_id"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", drive_id):
        raise ValueError("drive_id must contain only letters, digits, dot, underscore or hyphen")
    raw_frames = spec["frames"]
    if not raw_frames:
        raise ValueError("Manifest has no frames")
    raw_gps = [row for row in spec.get("gps", [])
               if row.get("lat") not in (None, "") and row.get("lon") not in (None, "")
               and (row.get("ts") or row.get("offset_s") not in (None, ""))]
    anchors = [parse_time(x["ts"]) - timedelta(seconds=float(x["offset_s"] or 0))
               for x in [*raw_frames, *raw_gps] if x.get("ts") and x.get("offset_s") not in (None, "")]
    timestamps = [parse_time(x["ts"]) for x in [*raw_frames, *raw_gps] if x.get("ts")]
    start = parse_time(spec["started_at"]) if spec.get("started_at") else (
        min(anchors) if anchors else min(timestamps, default=EPOCH)
    )
    clock_known = bool(spec.get("started_at") or timestamps)
    frame_step = float(spec.get("frame_interval_s", 1))
    gap = float(spec.get("max_gps_gap_s", 15))
    if not all(math.isfinite(x) and x > 0 for x in (gap, frame_step)):
        raise ValueError("Frame interval and GPS gap must be finite and positive")
    # Missing frame offsets follow neighboring timestamps, or the configured cadence
    # when only one (or no) side is available. Chronology follows manifest order.
    indexed_offsets = [(i, timed_items([item], start)[0]["offset_s"]) for i, item in enumerate(raw_frames)
                       if item.get("offset_s") not in (None, "") or item.get("ts")]
    positions = [i for i, _ in indexed_offsets]
    frame_rows = []
    for i, item in enumerate(raw_frames):
        cursor = bisect_left(positions, i)
        if cursor < len(positions) and positions[cursor] == i:
            pts = indexed_offsets[cursor][1]
        elif cursor and cursor < len(positions):
            left, low = indexed_offsets[cursor - 1]
            right, high = indexed_offsets[cursor]
            pts = low + (high - low) * (i - left) / (right - left)
        elif cursor:
            left, low = indexed_offsets[cursor - 1]
            pts = low + (i - left) * frame_step
        elif positions:
            right, high = indexed_offsets[0]
            pts = max(0, high - (right - i) * frame_step)
        else:
            pts = i * frame_step
        frame_rows.append(dict(item, offset_s=pts))
    frames = sorted(frame_rows, key=lambda item: item["offset_s"])
    points = sorted(timed_items(raw_gps, start), key=lambda item: item["offset_s"])
    if any(not math.isfinite(x["offset_s"]) or x["offset_s"] < 0 for x in frames + points):
        raise ValueError("Offsets must be finite and nonnegative")
    offsets = [p["offset_s"] for p in points]
    if len(set(offsets)) != len(offsets):
        raise ValueError("GPS offsets must be unique")
    route = [coordinates(*point) for point in spec.get("route_points", [])]
    if route and len(set(route)) < 2:
        raise ValueError("Route fallback needs at least two distinct coordinates")
    duration = float(spec.get("duration_s", frames[-1]["offset_s"]))
    if not math.isfinite(duration) or duration < frames[-1]["offset_s"]:
        raise ValueError("Duration must cover the final frame offset")
    drive = dict(id=drive_id, started_at=start.isoformat(), route=spec.get("route_name"),
                 vehicle_id=spec.get("vehicle_id"), video_ref=spec.get("video_ref", "extracted frames"),
                 privacy_reviewed=spec.get("privacy_reviewed") is True, clock_known=clock_known,
                 dataset=spec.get("dataset", "field"))
    previous = db.execute("SELECT payload FROM drives WHERE id=?", (drive_id,)).fetchone()
    if previous and json.loads(previous[0]) != drive:
        raise ValueError(f"Drive {drive_id} already exists with different metadata; use a new drive ID")
    put(db, "drives", {"id": drive_id}, drive)
    for p in points:
        p["point"] = coordinates(p.pop("lat"), p.pop("lon"))
        p["ts"] = (start + timedelta(seconds=p["offset_s"])).isoformat()
        for key in ("heading_deg", "speed_kph", "accuracy_m"):
            p[key] = float(p[key]) if p.get(key) not in (None, "") else None
            if p[key] is not None and (not math.isfinite(p[key]) or p[key] < 0 or (key == "heading_deg" and p[key] >= 360)):
                raise ValueError(f"Invalid {key}: {p[key]}")
        attach_road(p, road_index)
        put(db, "gps_points", {"drive_id": drive_id, "offset_s": p["offset_s"]}, p)
    seen = set()
    image_root = image_root.resolve()
    for index, item in enumerate(frames):
        pts = item["offset_s"]
        frame_id = item.get("frame_id", f"{drive_id}_f{index:06d}")
        existing = db.execute("SELECT drive_id, payload FROM frames WHERE id=?", (frame_id,)).fetchone()
        if frame_id in seen or (existing and existing["drive_id"] != drive_id):
            raise ValueError(f"Duplicate frame ID: {frame_id}")
        seen.add(frame_id)
        path = (image_root / item["image"]).resolve()
        if not path.is_relative_to(image_root):
            raise ValueError("Image paths must stay within the specified frames directory")
        with Image.open(path) as image:
            width, height = image.size
            image.verify()
        estimate = gps_estimate(points, offsets, pts, gap)
        if estimate is None and route:
            point, bearing = point_on_route(route, pts / duration if duration else 0)
            estimate = point, bearing, None, None, "route_estimate"
        point, bearing, speed, accuracy, method = estimate or (None, None, None, None, "unknown")
        ts = parse_time(item["ts"]) if item.get("ts") else start + timedelta(seconds=pts)
        weather = None
        if public is not None and clock_known:
            hour = ts.replace(minute=0, second=0, microsecond=0)
            weather = public.execute(
                "SELECT observed_at, condition FROM weather_hourly WHERE observed_at >= ? AND observed_at < ? "
                "ORDER BY observed_at DESC LIMIT 1",
                (hour.strftime("%Y-%m-%dT%H:%M:%SZ"), (hour + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")),
            ).fetchone()
        frame = attach_road(dict(
            id=frame_id, drive_id=drive_id, ts=ts.isoformat(), offset_s=pts, index=index, image_path=str(path),
            image_ref=path.relative_to(image_root).as_posix(), width=width, height=height, point=point,
            heading_deg=bearing, speed_kph=speed, accuracy_m=accuracy, location_source=method,
            clock_known=clock_known, weather_hour=weather[0] if weather else None,
            weather_condition=weather[1] if weather else None,
        ), road_index)
        if existing and json.loads(existing["payload"]) != json.loads(json.dumps(frame)):
            raise ValueError(f"Frame {frame_id} already exists with different metadata; use a new drive ID")
        put(db, "frames", {"id": frame_id, "drive_id": drive_id}, frame)
    return len(seen)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--frames-dir", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--postgis", help="Optional libpq DSN, or use 'env' for PG* variables")
    args = parser.parse_args()
    spec = json.loads(args.manifest.read_text())
    if spec.get("gps_csv"):
        with (args.manifest.parent / spec["gps_csv"]).open(newline="") as file:
            spec["gps"] = list(csv.DictReader(file))
    with baseline(args.baseline) as public, workspace(args.db) as db:
        print(f"Registered {ingest(db, spec, args.frames_dir, roads(public), public)} frames")
    if args.postgis:
        from ingestion.postgis import sync
        sync(args.db, args.postgis)


if __name__ == "__main__":
    main()
