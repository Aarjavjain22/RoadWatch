"""Build the offline meeting demo from the supplied Buffalo evaluation and public snapshot."""

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image, ImageFilter

from ingestion.geo import point_on_route
from ingestion.ingest_drive import ingest
from ingestion.replay_drive import frame_event
from ingestion.store import baseline, payloads, roads, workspace
from mapping.export_dashboard import feed, html
from mapping.hazard_mapper import cluster, record, records


def stage_scene(source, target):
    """Only the coarse, privacy-filtered derivative enters the demo workspace."""
    with Image.open(source) as image:
        size = image.size
        scene = image.convert("RGB")
        scene.thumbnail((48, 30))
        scene.filter(ImageFilter.GaussianBlur(.65)).resize(size, Image.Resampling.NEAREST).save(target)


def add_scenario(db, images, road_index, route):
    stub = images / "scenario.png"
    Image.new("RGB", (320, 180), "#19221c").save(stub)
    first = datetime(2025, 10, 6, 14, tzinfo=timezone.utc)
    for i in range(4):
        drive_id = f"meeting-pass-{i+1}"
        site_fractions = (.18 + .003 * i, .69 + .003 * i) if i < 2 else (.161, .20)
        positions = [point_on_route(route, t) for t in site_fractions]
        ingest(db, dict(
            drive_id=drive_id, dataset="meeting-fixture", vehicle_id=f"demo-vehicle-{i+1}",
            started_at=(first + timedelta(days=i)).isoformat(), route_name="Main Street (simulation)",
            frames=[dict(image=stub.name, offset_s=j * 12) for j in range(len(positions))],
            gps=[dict(offset_s=j*12, lat=point[0], lon=point[1], heading_deg=bearing, accuracy_m=4)
                 for j, (point, bearing) in enumerate(positions)],
        ), images, road_index)
        drive = next(d for d in payloads(db, "drives") if d["id"] == drive_id)
        for frame in (f for f in payloads(db, "frames") if f["drive_id"] == drive_id):
            event = frame_event(frame, drive, "meeting-fixture")
            detections = [] if i >= 2 else [dict(
                obs_id=frame["id"] + "-box", hazard_type=("alligator" if frame["index"] == 0 else "pothole"),
                confidence=.87, x1=140, x2=170, y1=112, y2=131)]
            header = dict(schema_version="1.0", ts=event["ts"])
            lat, lon = frame["point"]
            record(db, dict(**header, type="DetectionBatch", batch_id=frame["id"], frame=event,
                model_version="simulated-evidence", outcome="success", detections=detections,
                frame_result=dict(**header, type="FrameResult", frame_id=frame["id"], vehicle_id=drive["vehicle_id"],
                                  lat=lat, lon=lon, status="damaged" if detections else "clean",
                                  n_detections=len(detections), latency_ms=0, image_ref=event["image_ref"],
                                  model_version="simulated-evidence")))


def build(public, frames, detections):
    way = public.execute("SELECT geometry_geojson FROM roads WHERE osm_way_id=1368285545").fetchone()
    if way is None:
        way = public.execute("SELECT geometry_geojson FROM roads WHERE name='Main Street' ORDER BY length_m DESC LIMIT 1").fetchone()
    if way is None:
        raise ValueError("The meeting demo needs a Main Street route in the Buffalo public snapshot")
    route = [(lat, lon) for lon, lat in json.loads(way[0])["coordinates"]]
    road_index = roads(public)
    samples = sorted(frames.glob("video_1_morning_*.jpg"))[:28]
    if not samples:
        raise ValueError("The bundled video_1_morning evaluation frames are required")
    with TemporaryDirectory(prefix="buffalo-demo-") as temp:
        directory = Path(temp)
        selected = []
        for sample in samples:
            stage_scene(sample, directory / sample.name)
            selected.append(dict(frame_id=sample.stem, image=sample.name,
                                 offset_s=int(sample.stem.rsplit("_", 1)[1]) / 1000))
        with workspace(directory / "demo.db") as db:
            ingest(db, dict(drive_id="buffalo-evaluation", dataset="buffalo-eval", route_points=route,
                            route_name="Illustrative Main Street corridor", privacy_reviewed=True,
                            frames=selected), directory, road_index)
            for item in records(detections):
                record(db, item)
            add_scenario(db, directory, road_index, route)
            cluster(db, road_index, public, drafts=True)
            return feed(db, public, thumbnails=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--frames-dir", type=Path, required=True)
    parser.add_argument("--detections", type=Path, required=True)
    parser.add_argument("--html", type=Path, required=True)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    with baseline(args.baseline) as public:
        data = build(public, args.frames_dir, args.detections)
    args.html.write_text(html(data), encoding="utf-8")
    if args.json:
        args.json.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f'{len(data["frames"])} frames, {len(data["detections"])} detections, '
          f'{len(data["hazards"])} pins, {len(data["drafts"])} draft records')


if __name__ == "__main__":
    main()
