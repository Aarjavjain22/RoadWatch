import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import pytest
from PIL import Image

from ingestion.geo import Roads, distance
from ingestion.ingest_drive import ingest
from ingestion.replay_drive import frame_event
from ingestion.store import payloads, workspace
from mapping.export_dashboard import feed, html
from mapping.hazard_mapper import cluster, record
from schemas.events import FrameInput


LAT = 42.9
LON = -78.85


@pytest.fixture
def local(tmp_path):
    img = tmp_path / "frame.jpg"
    Image.new("RGB", (128, 96), "#455742").save(img)
    index = Roads([
        (1, "Main Street", [(LON, LAT - .003), (LON, LAT + .003)]),
        (2, "Parallel Road", [(LON + .0002, LAT - .003), (LON + .0002, LAT + .003)]),
    ])
    return tmp_path / "test.db", tmp_path, index


def add_drive(db, local, day, coordinates, *, prefix="drive"):
    _, directory, index = local
    drive_id = f"{prefix}-{day}"
    ingest(db, {
        "drive_id": drive_id, "vehicle_id": "vehicle-" + str(day), "privacy_reviewed": True,
        "started_at": f"2025-10-{day:02d}T12:00:00Z",
        "frames": [{"image": "frame.jpg", "offset_s": ordinal * 2} for ordinal in range(len(coordinates))],
        "gps": [{"offset_s": ordinal * 2, "lat": lat, "lon": lon, "heading_deg": 0, "accuracy_m": 5}
                for ordinal, (lat, lon) in enumerate(coordinates)],
    }, directory, index)
    return sorted((f for f in payloads(db, "frames") if f["drive_id"] == drive_id), key=lambda f: f["index"])


def evaluate(db, f, kind=None, *, failure=False, name="evidence"):
    drive = next(d for d in payloads(db, "drives") if d["id"] == f["drive_id"])
    frame = frame_event(f, drive, "test-session")
    header = {"schema_version": "1.0", "ts": frame["ts"]}
    fields = dict(**header, type="DetectionBatch", batch_id=f["id"], frame=frame,
                  model_version="test-model", outcome="failure" if failure else "success")
    if failure:
        return record(db, dict(fields, failure_code="decode"))
    lat, lon = f["point"]
    box = dict(obs_id=name, hazard_type=kind, confidence=.89, x1=60, y1=65, x2=80, y2=77)
    return record(db, dict(fields, detections=[box] if kind else [], frame_result=dict(
        **header, type="FrameResult", frame_id=f["id"], vehicle_id=drive["vehicle_id"],
        lat=lat, lon=lon, status="damaged" if kind else "clean", n_detections=int(bool(kind)),
        latency_ms=5, model_version="test-model", image_ref=frame["image_ref"],
    )))


def test_partial_gps_clock_and_route_fallback(local):
    path, directory, index = local
    with workspace(path) as db:
        spec = {
            "drive_id": "gps-and-route",
            "route_points": [(LAT, LON), (LAT + .002, LON)],
            "frames": [
                {"image": "frame.jpg", "offset_s": 0, "ts": "2025-10-06T12:00:00-04:00"},
                {"image": "frame.jpg"},
                {"image": "frame.jpg", "offset_s": 10},
                {"image": "frame.jpg", "offset_s": 30},
            ],
            "gps": [
                {"offset_s": 0, "lat": LAT, "lon": LON, "heading_deg": 350},
                {"offset_s": 5, "lat": "", "lon": ""},
                {"offset_s": 10, "lat": LAT + .001, "lon": LON, "heading_deg": 10},
            ],
        }
        assert ingest(db, spec, directory, index) == 4
        assert ingest(db, spec, directory, index) == 4
        f = sorted(payloads(db, "frames"), key=lambda row: row["offset_s"])
        assert [r["offset_s"] for r in f] == [0, 5, 10, 30]
        assert [r["location_source"] for r in f] == ["gps", "interpolated", "gps", "route_estimate"]
        assert f[1]["heading_deg"] == 0 and f[1]["osm_way_id"] == 1
        assert f[1]["ts"] == "2025-10-06T16:00:05+00:00"
        assert db.execute("SELECT count(*) FROM gps_points").fetchone()[0] == 2
        with pytest.raises(ValueError, match="frames directory"):
            ingest(db, {"drive_id": "x", "frames": [{"image": "../frame.jpg"}]}, directory, index)


def test_missing_clock_never_fabricates_gps_or_weather(local):
    path, directory, index = local
    with workspace(path) as db:
        ingest(db, {"drive_id": "playback", "frames": [{"image": "frame.jpg"}]}, directory, index)
        f = payloads(db, "frames")[0]
        d = payloads(db, "drives")[0]
        e = FrameInput.model_validate(frame_event(f, d, "fixture"))
        assert not e.clock_known and e.location_source == "unknown"
        assert e.telemetry.lat is None and e.weather_hour is None
        assert e.ts == datetime(1970, 1, 1, tzinfo=timezone.utc)


def test_merge_duplicate_evidence_and_lifecycle(local):
    path, _, index = local
    with workspace(path) as db:
        first = add_drive(db, local, 1, [(LAT, LON), (LAT + .00002, LON)])
        assert evaluate(db, first[0], "pothole")
        assert evaluate(db, first[0], "pothole")
        evaluate(db, first[1], "pothole")
        cluster(db, index, drafts=True)
        p = payloads(db, "hazards")[0]
        pin_id = p["id"]
        assert p["state"] == "candidate" and p["n_observations"] == 2 and p["n_drives"] == 1
        second = add_drive(db, local, 2, [(LAT + .00001, LON), (LAT, LON + .0002)])
        for f in second:
            evaluate(db, f, "pothole")
        cluster(db, index, drafts=True)
        hazards = {p["osm_way_id"]: p for p in payloads(db, "hazards")}
        assert hazards[1]["id"] == pin_id and hazards[1]["state"] == "confirmed"
        assert hazards[2]["state"] == "candidate"
        draft = payloads(db, "drafts")[0]
        assert draft["status"] == "pending_review" and draft["needs_human_approval"]
        # Failed and missing frames are gaps, even with clean frames on either side.
        partial = add_drive(db, local, 3, [(LAT - .00006, LON), (LAT, LON), (LAT + .00006, LON)])
        evaluate(db, partial[0])
        evaluate(db, partial[1], failure=True)
        evaluate(db, partial[2])
        for day in (4, 5):
            drive = add_drive(db, local, day, [(LAT - .00006, LON), (LAT + .00006, LON)])
            for f in drive:
                evaluate(db, f)
            cluster(db, index, drafts=True)
            p = next(p for p in payloads(db, "hazards") if p["id"] == pin_id)
            assert p["n_clean_drives"] == day - 3
            assert p["state"] == ("confirmed" if day == 4 else "resolved")
        assert [h["state"] for h in p["history"]] == ["candidate", "confirmed", "resolved"]
        assert payloads(db, "drafts")[0]["status"] == "closed_after_resolution"


def test_route_estimates_remain_candidates_and_are_exportable(local):
    path, directory, index = local
    with workspace(path) as db:
        for i in (1, 2):
            ingest(db, {
                "drive_id": f"route-{i}", "dataset": "field<script>",
                "privacy_reviewed": i == 1, "route_points": [(LAT, LON), (LAT + .001, LON)],
                "frames": [{"image": "frame.jpg", "frame_id": f"frame-{i}"}],
            }, directory, index)
            record(db, {"frame_id": f"frame-{i}", "damage_type": "Pothole", "confidence": .9})
        cluster(db, index, drafts=True)
        data = feed(db, None, thumbnails=True)
        assert data["hazards"][0]["state"] == "candidate"
        assert data["hazards"][0]["uncertainty_m"] >= 75
        assert not data["drafts"]
        assert len(data["detections"]) == 2
        assert sum("thumbnail" in f for f in data["frames"]) == 1
        assert all("image_path" not in f for f in data["frames"])
        page = html(data)
        assert "field\\u003cscript>" in page
        assert "http" not in page.replace("http://www.w3.org/2000/svg", "")
        assert "/*BUFFALO_DATA*/" not in page


def test_local_commands_generate_jsonl_and_html(local):
    path, _, _ = local
    with workspace(path) as db:
        frames = add_drive(db, local, 3, [(LAT, LON), (LAT + .00004, LON)])
    root = path.parent
    jsonl = root / "frames.jsonl"
    target = root / "demo.html"
    assert not any(p.exists() for p in (jsonl, target))
    subprocess.run([sys.executable, "-m", "ingestion.replay_drive", "--db", str(path), "--drive", "drive-3",
                    "--jsonl", str(jsonl)], check=True, env=os.environ)
    assert [json.loads(line)["frame_id"] for line in jsonl.read_text().splitlines()] == [f["id"] for f in frames]
    subprocess.run([sys.executable, "-m", "mapping.export_dashboard", "--db", str(path), "--html", str(target)],
                   check=True, env=os.environ)
    assert "<!doctype html>" in target.read_text()
