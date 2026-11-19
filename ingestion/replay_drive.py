"""Export v1 frame events as JSONL and optionally replay them into Kafka."""

import argparse
import base64
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from ingestion.store import payloads, workspace
from schemas.events import FrameInput


def load_drive(db, drive_id):
    drives = {d["id"]: d for d in payloads(db, "drives")}
    if drive_id not in drives:
        raise ValueError(f"Unknown drive: {drive_id}")
    return drives[drive_id], sorted(
        (f for f in payloads(db, "frames") if f["drive_id"] == drive_id),
        key=lambda f: (f["offset_s"], f["index"]),
    )


def frame_event(frame, drive, session):
    stamp = datetime.now(timezone.utc).isoformat()
    lat, lon = frame["point"] or (None, None)
    quality = "unknown" if lat is None else ("matched" if frame.get("osm_way_id") else "unmatched")
    return FrameInput.model_validate({
        "schema_version": "1.0", "type": "FrameInput", "ts": frame["ts"],
        "frame_id": frame["id"], "drive_id": drive["id"], "image_ref": frame["image_ref"],
        "width": frame["width"], "height": frame["height"], "frame_index": frame["index"],
        "pts_s": frame["offset_s"], "emitted_at": stamp, "replay_session_id": session,
        "gps_quality": quality, "weather_hour": frame["weather_hour"],
        "location_source": frame["location_source"], "clock_known": frame["clock_known"],
        "telemetry": {
            "schema_version": "1.0", "type": "VehicleState", "ts": frame["ts"],
            "vehicle_id": drive["vehicle_id"], "lat": lat, "lon": lon,
            "osm_way_id": frame.get("osm_way_id"), "heading_deg": frame["heading_deg"],
            "speed_kph": frame["speed_kph"], "gps_accuracy_m": frame["accuracy_m"],
        },
    }).model_dump(mode="json")


def kafka_replay(server, frames, drive, session, rate, legacy):
    from kafka import KafkaProducer

    producer = KafkaProducer(bootstrap_servers=server, acks="all", max_request_size=5_000_000,
                             value_serializer=lambda data: json.dumps(data, allow_nan=False).encode())
    try:
        start = time.monotonic()
        initial = frames[0]["offset_s"]
        for frame in frames:
            time.sleep(max(0, start + (frame["offset_s"] - initial) / rate - time.monotonic()))
            event = frame_event(frame, drive, session)
            key = f'{session}:{drive["id"]}'.encode()
            producer.send("roadwatch.frames", key=key, value=event).get(timeout=30)
            if legacy:
                data = base64.b64encode(Path(frame["image_path"]).read_bytes()).decode("ascii")
                producer.send("raw_frames", key=frame["id"].encode(), value={
                    "event_version": "1.0", "frame_id": frame["id"], "trace_id": frame["id"],
                    "camera_id": drive["vehicle_id"] or drive["id"], "timestamp": event["ts"],
                    "source_image": frame["image_ref"], "road_segment": str(frame.get("osm_way_id", "unmatched")),
                    "image_b64": data,
                }).get(timeout=30)
    finally:
        producer.close(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--drive", required=True)
    parser.add_argument("--jsonl", type=Path, required=True)
    parser.add_argument("--kafka", help="Optional broker(s), comma-separated")
    parser.add_argument("--rate", type=float, default=1)
    parser.add_argument("--legacy", action="store_true", help="Also feed the existing detector via raw_frames")
    args = parser.parse_args()
    if not 1 <= args.rate <= 10 or (args.legacy and not args.kafka):
        parser.error("Use a replay rate of 1–10 and supply --kafka with --legacy")
    with workspace(args.db) as db:
        drive, frames = load_drive(db, args.drive)
    if not frames:
        parser.error("Drive has no frames")
    if args.kafka and not drive["privacy_reviewed"]:
        parser.error("Kafka replay requires privacy_reviewed=true in the manifest")
    session = uuid4().hex
    temp = args.jsonl.with_name(f".{args.jsonl.name}.{session}.tmp")
    try:
        with temp.open("w") as file:
            for frame in frames:
                file.write(json.dumps(frame_event(frame, drive, session)) + "\n")
        os.replace(temp, args.jsonl)
    finally:
        temp.unlink(missing_ok=True)
    print(f"Exported {len(frames)} frames to {args.jsonl}", flush=True)
    if args.kafka:
        kafka_replay(args.kafka, frames, drive, session, args.rate, args.legacy)


if __name__ == "__main__":
    main()
