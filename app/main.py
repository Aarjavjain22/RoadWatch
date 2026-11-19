"""RoadWatch local UI — run with:  python -m uvicorn app.main:app --port 8080"""
from __future__ import annotations

import json
import shutil
import threading
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import datasets, pipeline, replay, store
from app.config import (
    EVIDENCE_DIR,
    FRAMES_DIR,
    REPORTS_DIR,
    SAMPLE_FPS,
    STREAM_FRAMES_DIR,
    UI_DIR,
    VIDEOS_DIR,
    WEATHER_CLS_WEIGHTS,
)

STATIC_DIR = Path(__file__).parent / "static"
DAMAGE_TYPES = ["Longitudinal", "Transverse", "Alligator", "Pothole", "Other"]
SEVERITIES = ["low", "medium", "high"]

app = FastAPI(title="RoadWatch local UI", version="1.0")


@app.on_event("startup")
def startup() -> None:
    store.init_db()
    replay.start_worker()
    threading.Thread(target=datasets.inventory, name="dataset-scan", daemon=True).start()


# ---------------------------------------------------------------- models

class ModelChoice(BaseModel):
    name: str


@app.get("/api/models")
def list_models():
    return {
        "models": replay.detection_models(),
        "active": replay.active_model(),
        "weather_classifier": WEATHER_CLS_WEIGHTS.exists(),
        "device": replay.resolve_device(),
        "sample_fps": SAMPLE_FPS,
    }


@app.post("/api/models/active")
def set_active_model(choice: ModelChoice):
    if choice.name not in replay.detection_models():
        raise HTTPException(404, f"no weights named {choice.name}")
    store.set_setting("active_model", choice.name)
    return {"active": choice.name}


# ---------------------------------------------------------------- replay

def _video_url(v: dict) -> str:
    if v.get("source_path"):
        return f"/api/videos/{v['id']}/media"
    return f"/media/videos/{v['filename']}"


def _video_summary(v: dict) -> dict:
    counts = store.one(
        "SELECT COUNT(DISTINCT h.id) AS hazards, COUNT(e.id) AS photos "
        "FROM hazards h LEFT JOIN evidence e ON e.hazard_id = h.id WHERE h.video_id=?",
        (v["id"],),
    )
    return {**v, **counts, "url": _video_url(v), "external": bool(v.get("source_path"))}


@app.get("/api/videos")
def list_videos(q: str = ""):
    vids = store.rows("SELECT * FROM videos ORDER BY created_at DESC, id DESC")
    if q:
        needle = q.lower()
        vids = [v for v in vids if needle in (v["original_name"] or v["filename"]).lower()]
    return [_video_summary(v) for v in vids]


@app.post("/api/videos")
def upload_video(file: UploadFile = File(...)):
    if Path(file.filename or "").suffix.lower() not in replay.VIDEO_EXTS:
        raise HTTPException(400, f"unsupported file type; use one of {sorted(replay.VIDEO_EXTS)}")
    tmp_dir = UI_DIR / "uploads"
    tmp_dir.mkdir(exist_ok=True)
    tmp = tmp_dir / f"{uuid.uuid4().hex}{Path(file.filename).suffix.lower()}"
    with tmp.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    try:
        video_id = replay.ingest(tmp, file.filename)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise HTTPException(422, f"could not read video: {exc}") from exc
    return _video_summary(store.one("SELECT * FROM videos WHERE id=?", (video_id,)))


@app.post("/api/videos/refresh")
def refresh_videos():
    return {"added": replay.refresh_library()}


@app.get("/api/videos/{video_id}/media")
def video_media(video_id: int):
    v = store.one("SELECT * FROM videos WHERE id=?", (video_id,))
    if not v:
        raise HTTPException(404, "video not found")
    path = replay.video_file_path(v)
    if not path.is_file():
        raise HTTPException(404, "video file missing on disk")
    return FileResponse(path, media_type="video/mp4", filename=v["original_name"] or path.name)


@app.get("/api/videos/{video_id}")
def get_video(video_id: int):
    v = store.one("SELECT * FROM videos WHERE id=?", (video_id,))
    if not v:
        raise HTTPException(404, "video not found")
    return _video_summary(v)


@app.post("/api/videos/{video_id}/analyze")
def reanalyze(video_id: int):
    if not store.one("SELECT id FROM videos WHERE id=?", (video_id,)):
        raise HTTPException(404, "video not found")
    replay.enqueue(video_id)
    return {"queued": video_id}


@app.get("/api/videos/{video_id}/detections")
def video_detections(video_id: int):
    return [
        {"t": r["t"], "boxes": json.loads(r["boxes"])}
        for r in store.rows("SELECT t, boxes FROM detections WHERE video_id=? ORDER BY t", (video_id,))
    ]


@app.get("/api/videos/{video_id}/hazards")
def video_hazards(video_id: int):
    hazards = store.rows("SELECT * FROM hazards WHERE video_id=? ORDER BY first_t", (video_id,))
    for h in hazards:
        h["evidence"] = [
            {**e, "url": f"/media/evidence/{e['path']}"}
            for e in store.rows("SELECT path, t, conf FROM evidence WHERE hazard_id=? ORDER BY t", (h["id"],))
        ]
    return hazards


# ---------------------------------------------------------------- reports

@app.get("/api/reports")
def list_reports():
    reports = store.rows("SELECT * FROM reports ORDER BY created_at DESC, id DESC")
    for r in reports:
        r["photo_url"] = f"/media/reports/{r['photo']}" if r["photo"] else None
    return reports


@app.post("/api/reports")
def create_report(
    damage_type: str = Form(...),
    severity: str = Form(...),
    location: str = Form(""),
    lat: float | None = Form(None),
    lon: float | None = Form(None),
    notes: str = Form(""),
    photo: UploadFile | None = File(None),
):
    if damage_type not in DAMAGE_TYPES or severity not in SEVERITIES:
        raise HTTPException(400, "invalid damage type or severity")
    photo_name = None
    if photo is not None and photo.filename:
        ext = Path(photo.filename).suffix.lower()
        if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
            raise HTTPException(400, "photo must be jpg, png or webp")
        photo_name = f"{uuid.uuid4().hex}{ext}"
        with (REPORTS_DIR / photo_name).open("wb") as out:
            shutil.copyfileobj(photo.file, out)
    report_id = store.execute(
        "INSERT INTO reports (damage_type, severity, location, lat, lon, notes, photo) VALUES (?,?,?,?,?,?,?)",
        (damage_type, severity, location.strip(), lat, lon, notes.strip(), photo_name),
    )
    return {"id": report_id}


# ---------------------------------------------------------------- calibration

class Calibration(BaseModel):
    camera_height_m: float = Field(gt=0.2, le=10)
    vfov_deg: float = Field(gt=10, lt=150)
    horizon_frac: float = Field(ge=0.05, le=0.95)


@app.get("/api/calibration")
def get_calibration():
    return store.get_calibration()


@app.put("/api/calibration")
def put_calibration(cal: Calibration):
    store.set_setting("calibration", cal.model_dump())
    return store.get_calibration()


# ---------------------------------------------------------------- pipeline (Postgres)

@app.get("/api/live")
def live():
    try:
        return {"online": True, **pipeline.live_snapshot()}
    except pipeline.PipelineOffline as exc:
        return {"online": False, "error": str(exc)}


@app.get("/api/weather-insights")
def weather_insights():
    replay_by_condition = store.rows(
        "SELECT COALESCE(visibility_condition, 'untagged') AS condition, COUNT(*) AS hazards, "
        "ROUND(AVG(best_conf), 3) AS avg_confidence FROM hazards GROUP BY 1 ORDER BY hazards DESC"
    )
    try:
        return {"online": True, "replay_by_condition": replay_by_condition, **pipeline.weather_insights()}
    except pipeline.PipelineOffline as exc:
        return {"online": False, "error": str(exc), "replay_by_condition": replay_by_condition}


# ---------------------------------------------------------------- datasets & records

@app.get("/api/datasets")
def dataset_inventory(refresh: bool = False):
    records = store.one(
        "SELECT (SELECT COUNT(*) FROM videos) AS videos, "
        "(SELECT COUNT(*) FROM videos WHERE status='done') AS videos_analyzed, "
        "(SELECT COUNT(*) FROM hazards) AS hazards, (SELECT COUNT(*) FROM evidence) AS evidence_photos, "
        "(SELECT COUNT(*) FROM reports) AS reports"
    )
    return {"inventory": datasets.inventory(force=refresh), "records": records,
            "results": datasets.model_results()}


# ---------------------------------------------------------------- static

app.mount("/media/videos", StaticFiles(directory=VIDEOS_DIR), name="videos")
app.mount("/media/evidence", StaticFiles(directory=EVIDENCE_DIR), name="evidence")
app.mount("/media/reports", StaticFiles(directory=REPORTS_DIR), name="reports")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/media/frames/{name}")
def stream_frame(name: str):
    """Pipeline frames: replayed images live in data/frames, sampled video frames in data/stream_frames."""
    safe = Path(name).name
    for directory in (STREAM_FRAMES_DIR, FRAMES_DIR):
        path = directory / safe
        if path.is_file():
            return FileResponse(path)
    raise HTTPException(404, "frame not available (video previews keep only the latest few hundred)")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
