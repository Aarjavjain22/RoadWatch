"""Ingest detection batches or legacy detections and persist Buffalo hazard episodes."""

import argparse
import csv
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import get_args

from ingestion.geo import distance, offset, xy
from ingestion.store import baseline, payloads, put, roads, workspace
from schemas.events import DetectionBatch, HazardType


def stable_id(prefix, *parts):
    return prefix + hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:20]


def records(path):
    with path.open(newline="") as file:
        if path.suffix.lower() == ".csv":
            for row in csv.DictReader(file):
                if "strip_view" not in row.get("group", ""):
                    yield row
        else:
            for line in file:
                if line.strip():
                    yield json.loads(line)


def insert_observation(db, frame_id, row, model, ordinal=0):
    kind = str(row.get("hazard_type") or row.get("damage_type") or "").lower()
    if kind not in get_args(HazardType):
        return
    confidence = float(row["confidence"])
    box = row.get("bbox") or [float(row[k]) for k in ("x1", "y1", "x2", "y2") if row.get(k) is not None]
    if box and (len(box) != 4 or not all(math.isfinite(float(x)) for x in box)
                or not (0 <= float(box[0]) < float(box[2]) and 0 <= float(box[1]) < float(box[3]))):
        raise ValueError(f"Invalid box for frame {frame_id}")
    if not 0 <= confidence <= 1:
        raise ValueError(f"Invalid detection confidence for {frame_id}")
    range_m = row.get("distance_m")
    range_m = float(range_m) if range_m not in ("", None) else None
    if range_m is not None and not 0 <= range_m <= 80:
        raise ValueError("Calibrated distance must be in the 0–80 m range")
    incoming_id = row.get("obs_id") or row.get("event_id")
    obs_id = stable_id("obs_", frame_id, model, incoming_id or [kind, box, ordinal])
    item = dict(id=obs_id, frame_id=frame_id, hazard_type=kind, confidence=confidence,
                bbox=[float(x) for x in box], model_version=model, distance_m=range_m)
    put(db, "observations", {"id": obs_id, "frame_id": frame_id}, item)


def record(db, event):
    is_batch = event.get("type") == "DetectionBatch"
    frame_id = event["frame"]["frame_id"] if is_batch else event["frame_id"]
    if not db.execute("SELECT 1 FROM frames WHERE id=?", (frame_id,)).fetchone():
        return False
    if is_batch:
        batch = DetectionBatch.model_validate(event)
        model = batch.model_version
        prior = db.execute("SELECT payload FROM frame_results WHERE frame_id=?", (frame_id,)).fetchone()
        if prior:
            old = json.loads(prior[0])
            if old["model_version"] != model:
                raise ValueError(f"Multiple models for frame {frame_id}; use a separate workspace")
            if old["outcome"] == "success":
                return True
        put(db, "frame_results", {"frame_id": frame_id}, dict(
            frame_id=frame_id, model_version=model, outcome=batch.outcome,
            status=batch.frame_result.status if batch.frame_result else "failed"))
        for ordinal, item in enumerate(batch.detections):
            insert_observation(db, frame_id, item.model_dump(), model, ordinal)
    else:
        if event.get("event_version", "1.0") != "1.0":
            raise ValueError("Unsupported legacy event version")
        insert_observation(db, frame_id, event, event.get("model_version") or "legacy-buffalo")
    return True


def position(frame, item, road_index):
    if frame["point"] is None:
        return None
    point = frame["point"]
    bearing = frame["heading_deg"]
    projected = item["distance_m"] is not None and bearing is not None
    if projected:
        point = offset(point, bearing, item["distance_m"])
    match = road_index.snap(point, preferred=frame.get("osm_way_id"))
    if match is None:
        return None
    actual = frame["location_source"] in {"gps", "interpolated"}
    method = "route estimate" if not actual else ("calibrated range" if projected else "camera position proxy")
    uncertainty = max(8, frame["accuracy_m"] or 8) + match[3] + (5 if projected else 12)
    return dict(point=match[0], osm_way_id=match[1], street=match[2],
                uncertainty_m=round(uncertainty if actual else max(75, uncertainty), 1), method=method)


def eligible(frame):
    return (frame["clock_known"] and frame["location_source"] in {"gps", "interpolated"}
            and (frame["accuracy_m"] is None or frame["accuracy_m"] <= 20))


def build_draft(pin, public):
    nearest = None
    if public is not None:
        for hit in public.execute(
            "SELECT case_number, lat, lon, status FROM city_311_road_requests "
            "WHERE osm_way_id=? AND lower(type) LIKE '%pothole%' AND lat IS NOT NULL AND lon IS NOT NULL",
            (pin["osm_way_id"],),
        ):
            gap = distance(pin["point"], (hit["lat"], hit["lon"]))
            if gap <= 30 and "closed" not in (hit["status"] or "").lower():
                if nearest is None or (gap, hit["case_number"]) < nearest:
                    nearest = gap, hit["case_number"]
    if nearest:
        pin["linked_311_case"] = nearest[1]
        return None
    return dict(id=stable_id("draft_", pin["id"]), hazard_id=pin["id"], status="pending_review",
                category="Pothole Issue", street=pin["street"], point=pin["point"], needs_human_approval=True,
                description=f'Possible pothole near {pin["street"] or "an unnamed road"} from repeated drive sightings.',
                observation_count=pin["n_observations"], first_seen=pin["first_seen"], last_seen=pin["last_seen"],
                uncertainty_m=pin["uncertainty_m"], dataset=pin["dataset"])


def covered_crossing(frames, clean, pin, radius):
    """Evidence spans both sides of the pin, with all sampled frames in between clean."""
    x, y = xy(*pin["point"])
    bearing = math.radians(pin["bearing"])
    window = []
    for frame in frames:
        point = frame.get("matched_point")
        if (frame["ts"] <= pin["last_seen"] or frame.get("osm_way_id") != pin["osm_way_id"]
                or point is None or distance(point, pin["point"]) > radius):
            continue
        a, b = xy(*point)
        along = (a - x) * math.sin(bearing) + (b - y) * math.cos(bearing)
        same_direction = (frame["heading_deg"] is not None
                          and abs((frame["heading_deg"] - pin["bearing"] + 180) % 360 - 180) <= 60)
        if not same_direction or frame["id"] not in clean:
            return None
        window.append((along, frame))
    if window and window[0][0] <= -2 and window[-1][0] >= 2:
        return window[-1][1]
    return None


def cluster(db, road_index, public=None, radius=12, confirm_drives=2, resolve_drives=2, drafts=False):
    """Recompute deterministically so redelivery or out-of-order arrivals do not add evidence."""
    drives = sorted(payloads(db, "drives"), key=lambda drive: (drive["started_at"], drive["id"]))
    frames_by_drive = defaultdict(list)
    items_by_frame = defaultdict(list)
    evaluated = {r["frame_id"]: r for r in payloads(db, "frame_results")}
    for frame in payloads(db, "frames"):
        frames_by_drive[frame["drive_id"]].append(frame)
    for item in payloads(db, "observations"):
        items_by_frame[item["frame_id"]].append(item)
    pins = defaultdict(list)
    members = {}
    for drive in drives:
        frames = sorted(frames_by_drive[drive["id"]], key=lambda f: (f["ts"], f["index"]))
        seen = set()
        clean = set()
        for frame in frames:
            items = items_by_frame[frame["id"]]
            result = evaluated.get(frame["id"], {})
            if (result.get("status") == "clean" and not items and eligible(frame)
                    and frame.get("matched_point") and frame["heading_deg"] is not None):
                clean.add(frame["id"])
            for item in items:
                located = position(frame, item, road_index)
                item.update(point=located["point"] if located else None,
                            uncertainty_m=located["uncertainty_m"] if located else None)
                put(db, "observations", {"id": item["id"], "frame_id": item["frame_id"]}, item)
                if located is None:
                    continue
                key = drive["dataset"], item["hazard_type"], located["osm_way_id"]
                nearby = ((distance(pin["point"], located["point"]), pin["id"], pin) for pin in pins[key]
                          if pin["state"] != "resolved")
                match = min((x for x in nearby if x[0] <= radius), default=None)
                pin = match[2] if match else dict(
                    **located, id=stable_id("H-", drive["dataset"], item["id"]), state="candidate",
                    hazard_type=item["hazard_type"], dataset=drive["dataset"], first_seen=frame["ts"],
                    last_seen=frame["ts"], n_observations=0, max_confidence=0, drives=set(), vehicles=set(),
                    clean_drives=set(), bearing=frame["heading_deg"], history=[dict(state="candidate", ts=frame["ts"])],
                )
                if match is None:
                    pins[key].append(pin)
                seen.add(pin["id"])
                members[item["id"]] = pin["id"]
                pin["n_observations"] += 1
                pin["last_seen"] = max(pin["last_seen"], frame["ts"])
                pin["max_confidence"] = max(pin["max_confidence"], item["confidence"])
                pin["clean_drives"].clear()
                if eligible(frame) and item["confidence"] >= .25:
                    pin["drives"].add(drive["id"])
                    if drive["vehicle_id"]:
                        pin["vehicles"].add(drive["vehicle_id"])
                    if pin["method"] == "route estimate":
                        pin.update(point=located["point"], uncertainty_m=located["uncertainty_m"],
                                   method=located["method"], bearing=frame["heading_deg"])
                if pin["state"] == "candidate" and len(pin["drives"]) >= confirm_drives:
                    pin["state"] = "confirmed"
                    pin["history"].append(dict(state="confirmed", ts=frame["ts"]))
        for group in pins.values():
            for pin in group:
                if (pin["state"] != "confirmed" or pin["id"] in seen or pin["dataset"] != drive["dataset"]
                        or drive["id"] in pin["drives"] or pin["bearing"] is None):
                    continue
                crossing = covered_crossing(frames, clean, pin, radius)
                if crossing:
                    pin["clean_drives"].add(drive["id"])
                    if len(pin["clean_drives"]) >= resolve_drives:
                        pin["state"] = "resolved"
                        pin["history"].append(dict(state="resolved", ts=crossing["ts"]))
    db.execute("DELETE FROM hazard_members")
    db.execute("DELETE FROM hazards")
    db.execute("DELETE FROM drafts")
    count = 0
    for group in pins.values():
        for pin in group:
            pin["n_drives"] = len(pin.pop("drives"))
            pin["n_vehicles"] = len(pin.pop("vehicles"))
            pin["n_clean_drives"] = len(pin.pop("clean_drives"))
            pin["severity"] = "high" if pin["max_confidence"] >= .75 else "medium"
            if drafts and pin["state"] in {"confirmed", "resolved"} and pin["hazard_type"] == "pothole":
                draft = build_draft(pin, public)
                if draft:
                    if pin["state"] == "resolved":
                        draft["status"] = "closed_after_resolution"
                    put(db, "drafts", {"id": draft["id"]}, draft)
            put(db, "hazards", {"id": pin["id"]}, pin)
            count += 1
    db.executemany("INSERT INTO hazard_members VALUES (?, ?)", members.items())
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--detections", type=Path, help="v1/legacy JSONL or bundled detection CSV")
    source.add_argument("--kafka", help="Broker(s), comma-separated")
    parser.add_argument("--topic", choices=["roadwatch.detections", "road_damage_events"], default="roadwatch.detections")
    parser.add_argument("--group", default="roadwatch-buffalo-mapper")
    parser.add_argument("--idle-seconds", type=float, default=0, help="0 means keep consuming")
    parser.add_argument("--radius-m", type=float, default=12)
    parser.add_argument("--confirm-drives", type=int, default=2)
    parser.add_argument("--resolve-drives", type=int, default=2)
    parser.add_argument("--draft-311", action="store_true")
    parser.add_argument("--postgis", help="Optional libpq DSN, or 'env' to use PG* variables")
    args = parser.parse_args()
    if not (0 < args.radius_m <= 100 and args.confirm_drives >= 1 and args.resolve_drives >= 1
            and math.isfinite(args.idle_seconds) and args.idle_seconds >= 0):
        parser.error("Use a radius in (0, 100], positive drive counts and a nonnegative idle timeout")
    with baseline(args.baseline) as public:
        road_index = roads(public)

        def apply(events):
            with workspace(args.db) as db:
                accepted = sum(record(db, event) for event in events)
                count = cluster(db, road_index, public, args.radius_m, args.confirm_drives,
                                args.resolve_drives, args.draft_311)
            if args.postgis:
                from ingestion.postgis import sync
                sync(args.db, args.postgis)
            print(f"Accepted {accepted} detection records; projected {count} hazard pins", flush=True)

        if args.kafka:
            from kafka import KafkaConsumer
            from time import monotonic

            consumer = KafkaConsumer(args.topic, bootstrap_servers=args.kafka, group_id=args.group,
                                     enable_auto_commit=False, auto_offset_reset="earliest",
                                     value_deserializer=json.loads)
            try:
                last = monotonic()
                while True:
                    batches = consumer.poll(timeout_ms=1000, max_records=200)
                    if batches:
                        apply(msg.value for batch in batches.values() for msg in batch)
                        consumer.commit()
                        last = monotonic()
                    if args.idle_seconds and monotonic() - last >= args.idle_seconds:
                        break
            finally:
                consumer.close(autocommit=False)
        else:
            apply(records(args.detections) if args.detections else ())


if __name__ == "__main__":
    main()
