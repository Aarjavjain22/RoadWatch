"""Export a privacy-filtered Buffalo JSON feed and a standalone HTML dashboard."""

import argparse
import base64
import io
import json
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps

from ingestion.store import baseline, payloads, road_rows, workspace


def preview(path):
    """Embed a coarse scene thumbnail so details of faces and plates are obscured."""
    with Image.open(path) as image:
        scene = ImageOps.exif_transpose(image).convert("RGB")
        scene.thumbnail((48, 30))
        scene = scene.filter(ImageFilter.GaussianBlur(.65))
        stream = io.BytesIO()
        scene.save(stream, "JPEG", quality=48)
    return "data:image/jpeg;base64," + base64.b64encode(stream.getvalue()).decode()


def feed(db, public, thumbnails=False, preview_limit=12):
    drives = {drive["id"]: drive for drive in payloads(db, "drives")}
    pins = payloads(db, "hazards")
    observations = payloads(db, "observations")
    boxes = {}
    for item in observations:
        boxes.setdefault(item["frame_id"], []).append(item)
    frames = []
    for frame in sorted(payloads(db, "frames"), key=lambda f: (f["drive_id"], f["offset_s"])):
        drive = drives[frame["drive_id"]]
        row = {key: frame[key] for key in ("id", "drive_id", "ts", "offset_s", "width", "height",
                                            "point", "location_source", "clock_known")}
        row.update(dataset=drive["dataset"], street=frame.get("street"), detection_count=len(boxes.get(frame["id"], [])))
        frames.append(row)
    if thumbnails:
        by_id = {frame["id"]: frame for frame in payloads(db, "frames")}
        preview_rows = sorted(frames, key=lambda f: (-f["detection_count"], f["offset_s"]))
        for row in preview_rows:
            if preview_limit <= 0:
                break
            if drives[row["drive_id"]]["privacy_reviewed"]:
                row["thumbnail"] = preview(by_id[row["id"]]["image_path"])
                preview_limit -= 1

    points = [pin["point"] for pin in pins] or [f["point"] for f in frames if f["point"]]
    points = points or [(42.91, -78.85), (42.92, -78.83)]
    south = min(p[0] for p in points) - .002
    north = max(p[0] for p in points) + .002
    west = min(p[1] for p in points) - .003
    east = max(p[1] for p in points) + .003
    street_lines = []
    preferred = {pin["osm_way_id"] for pin in pins}
    for osm, name, line in road_rows(public):
        if (max(p[0] for p in line) >= west and min(p[0] for p in line) <= east
                and max(p[1] for p in line) >= south and min(p[1] for p in line) <= north):
            street_lines.append(dict(osm_way_id=osm, street=name, line=line))
    street_lines.sort(key=lambda x: (x["osm_way_id"] not in preferred, x["street"] is None, x["osm_way_id"]))
    cases = []
    counts = {}
    if public is not None:
        for table in ("roads", "city_311_road_requests", "city_311_potholes_hist"):
            counts[table] = public.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        cases = [dict(row) for row in public.execute("""
            SELECT case_number, type, status, created_at, lat, lon
            FROM city_311_road_requests WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?
            AND lower(type) LIKE '%pothole%' ORDER BY created_at DESC LIMIT 360
        """, (south, north, west, east))]
    return dict(schema_version="1.0", bounds=[south, west, north, east], baseline_counts=counts,
                roads=street_lines[:500], cases=cases, frames=frames, hazards=pins, detections=observations,
                drafts=payloads(db, "drafts"), results=payloads(db, "frame_results"))


def html(data):
    template = Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")
    # Neutralize all script terminators, including mixed-case HTML variants.
    embedded = json.dumps(data, separators=(",", ":"), ensure_ascii=True, allow_nan=False).replace("<", "\\u003c")
    return template.replace("/*BUFFALO_DATA*/", embedded)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--html", type=Path)
    parser.add_argument("--thumbnails", action="store_true", help="Embed coarse images from privacy-approved drives")
    args = parser.parse_args()
    if not (args.json or args.html):
        parser.error("Supply --json and/or --html")
    with baseline(args.baseline) as public, workspace(args.db) as db:
        data = feed(db, public, args.thumbnails)
    if args.json:
        args.json.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    if args.html:
        args.html.write_text(html(data), encoding="utf-8")
    print(f'{len(data["frames"])} frames, {len(data["hazards"])} pins, {len(data["drafts"])} draft records')


if __name__ == "__main__":
    main()
