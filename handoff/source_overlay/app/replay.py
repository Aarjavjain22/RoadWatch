"""Video ingest and offline YOLO analysis for the Detection replay page."""
from __future__ import annotations

import json
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import cv2
import imageio_ffmpeg

from app import store
from app.config import (
    CONF_THRESHOLD,
    DEVICE,
    EVIDENCE_DIR,
    EXTRA_VIDEO_LIBRARY_DIRS,
    MAX_EVIDENCE_PER_HAZARD,
    ROOT,
    SAMPLE_FPS,
    VIDEOS_DIR,
    WEATHER_CLS_WEIGHTS,
    WEIGHTS_DIR,
)
from app.geometry import HazardTracker, estimate_distance_m

sys.path.insert(0, str(ROOT / "streaming"))
from contracts import classify_visibility  # noqa: E402
from image_heuristics import compute_image_heuristics, crop_letterbox  # noqa: E402

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
BROWSER_CODECS = {"h264", "vp8", "vp9", "av1"}
BOX_COLOR = (75, 148, 232)  # BGR — matches UI accent (#4a90c4)
EVIDENCE_EVERY = 2  # save every Nth observation of a hazard until the cap is reached

_models: dict[str, object] = {}
_models_lock = threading.Lock()
_jobs: queue.Queue[int] = queue.Queue()


# ---------------------------------------------------------------- models

def detection_models() -> list[str]:
    return sorted(p.name for p in WEIGHTS_DIR.glob("*.pt") if not p.name.startswith("weather_cls"))


def active_model() -> str | None:
    available = detection_models()
    chosen = store.get_setting("active_model")
    if chosen in available:
        return chosen
    return "best.pt" if "best.pt" in available else (available[0] if available else None)


def resolve_device() -> str:
    if DEVICE != "auto":
        return DEVICE
    import torch

    return "0" if torch.cuda.is_available() else "cpu"


def get_model(name: str):
    from ultralytics import YOLO

    with _models_lock:
        if name not in _models:
            _models[name] = YOLO(str(WEIGHTS_DIR / name))
        return _models[name]


# ---------------------------------------------------------------- ingest

def ffmpeg_exe() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def video_codec(path: Path) -> str | None:
    proc = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)],
                          capture_output=True, text=True, errors="ignore")
    m = re.search(r"Video:\s*(\w+)", proc.stderr)
    return m.group(1).lower() if m else None


def transcode_to_h264(src: Path, dst: Path) -> None:
    """Re-encode to browser-playable H.264 (even dimensions, faststart, no audio)."""
    cmd = [
        ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "23", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", str(dst),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="ignore")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()[-400:]}")


def probe(path: Path) -> dict:
    cap = cv2.VideoCapture(str(path))
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        return {
            "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            "fps": round(fps, 2),
            "duration": round(frames / fps, 2) if frames else 0.0,
        }
    finally:
        cap.release()


def safe_stem(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(name).stem).strip("_") or "video"
    return stem[:60]


def video_file_path(video: dict) -> Path:
    if video.get("source_path"):
        return Path(video["source_path"])
    return VIDEOS_DIR / video["filename"]


def register_external(src: Path, *, queue_analysis: bool = True) -> int | None:
    """Add a browser-playable file already on disk (no copy). Returns id or None if skipped."""
    src = src.resolve()
    if src.suffix.lower() not in VIDEO_EXTS:
        return None
    row = store.one("SELECT id FROM videos WHERE source_path=?", (str(src),))
    if row:
        return row["id"]
    playable = src.suffix.lower() == ".mp4" and video_codec(src) in BROWSER_CODECS
    if not playable:
        return ingest(src, src.name)
    slug = f"lib_{safe_stem(src.name)}.mp4"
    n = 1
    while store.one("SELECT id FROM videos WHERE filename=?", (slug,)):
        slug = f"lib_{safe_stem(src.name)}_{n}.mp4"
        n += 1
    meta = probe(src)
    status = "queued" if queue_analysis else "pending"
    video_id = store.execute(
        "INSERT INTO videos (filename, original_name, width, height, fps, duration, status, source_path) "
        "VALUES (?,?,?,?,?,?, ?, ?)",
        (slug, src.name, meta["width"], meta["height"], meta["fps"], meta["duration"], status, str(src)),
    )
    if queue_analysis:
        enqueue(video_id)
    return video_id


def ingest(src: Path, original_name: str) -> int:
    """Make `src` browser-playable inside VIDEOS_DIR, register it, and queue analysis."""
    playable = src.suffix.lower() == ".mp4" and video_codec(src) in BROWSER_CODECS
    stem = safe_stem(original_name)
    dst = VIDEOS_DIR / f"{stem}.mp4"
    n = 1
    # A playable file may keep its own path; a transcode always needs a distinct output path.
    while dst.exists() and not (playable and dst.resolve() == src.resolve()):
        dst = VIDEOS_DIR / f"{stem}_{n}.mp4"
        n += 1

    if playable:
        if src.resolve() != dst.resolve():
            src.replace(dst)
    else:
        transcode_to_h264(src, dst)
        src.unlink(missing_ok=True)

    meta = probe(dst)
    video_id = store.execute(
        "INSERT INTO videos (filename, original_name, width, height, fps, duration, status) "
        "VALUES (?,?,?,?,?,?, 'queued')",
        (dst.name, original_name, meta["width"], meta["height"], meta["fps"], meta["duration"]),
    )
    enqueue(video_id)
    return video_id


def refresh_library() -> int:
    """Register new videos from data/videos/ and configured external library folders."""
    known = {r["filename"] for r in store.rows("SELECT filename FROM videos")}
    known_sources = {
        r["source_path"]
        for r in store.rows("SELECT source_path FROM videos WHERE source_path IS NOT NULL")
    }
    added = 0
    for p in sorted(VIDEOS_DIR.iterdir()):
        if p.suffix.lower() in VIDEO_EXTS and p.name not in known and not p.name.startswith("."):
            try:
                ingest(p, p.name)
                added += 1
            except Exception as exc:
                print(f"[replay] skipped {p.name}: {exc}")
    for lib_dir in EXTRA_VIDEO_LIBRARY_DIRS:
        if not lib_dir.is_dir():
            continue
        for p in sorted(lib_dir.iterdir()):
            if p.suffix.lower() not in VIDEO_EXTS or p.name.startswith("."):
                continue
            sp = str(p.resolve())
            if sp in known_sources:
                continue
            try:
                if register_external(p, queue_analysis=False) is not None:
                    added += 1
                    known_sources.add(sp)
            except Exception as exc:
                print(f"[replay] skipped {p}: {exc}")
    return added


# ---------------------------------------------------------------- analysis

def enqueue(video_id: int) -> None:
    store.execute("UPDATE videos SET status='queued', progress=0, error=NULL WHERE id=?", (video_id,))
    _jobs.put(video_id)


def _weather_tag(frame, weather_cls) -> dict:
    frame = crop_letterbox(frame)
    h = compute_image_heuristics(frame)
    ml_weather, ml_conf = None, 0.0
    if weather_cls is not None:
        probs = weather_cls.predict(frame, verbose=False)[0].probs
        ml_weather, ml_conf = weather_cls.names[int(probs.top1)], float(probs.top1conf)
    condition, _, _ = classify_visibility(
        h["brightness"], h["contrast"], h["blur_score"], h["glare_ratio"], "unknown", ml_weather, ml_conf
    )
    return {"visibility_condition": condition, "ml_weather": ml_weather,
            "ml_weather_conf": round(ml_conf, 3) if ml_weather else None}


def _annotate(frame, det: dict, track_id: int, distance_m: float | None):
    img = frame.copy()
    x1, y1, x2, y2 = (int(v) for v in det["box"])
    thick = max(2, img.shape[1] // 400)
    cv2.rectangle(img, (x1, y1), (x2, y2), BOX_COLOR, thick)
    label = f"{det['damage_type']} #{track_id} {det['confidence']:.0%}"
    if distance_m is not None:
        label += f" | {distance_m} m"
    scale = max(0.5, img.shape[1] / 1600)
    (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, scale, 2)
    ty = max(th + 8, y1)
    cv2.rectangle(img, (x1, ty - th - 8), (x1 + tw + 8, ty), BOX_COLOR, -1)
    cv2.putText(img, label, (x1 + 4, ty - 5), cv2.FONT_HERSHEY_SIMPLEX, scale, (20, 20, 20), 2)
    if img.shape[1] > 960:
        img = cv2.resize(img, (960, int(img.shape[0] * 960 / img.shape[1])))
    return img


def _clear_previous(video_id: int) -> None:
    for e in store.rows(
        "SELECT e.path FROM evidence e JOIN hazards h ON h.id = e.hazard_id WHERE h.video_id=?", (video_id,)
    ):
        (EVIDENCE_DIR / e["path"]).unlink(missing_ok=True)
    store.execute("DELETE FROM hazards WHERE video_id=?", (video_id,))
    store.execute("DELETE FROM detections WHERE video_id=?", (video_id,))


def analyze(video_id: int) -> None:
    video = store.one("SELECT * FROM videos WHERE id=?", (video_id,))
    model_name = active_model()
    if not video or not model_name:
        raise RuntimeError("video missing or no detection weights in model/weights/")

    store.execute("UPDATE videos SET status='analyzing', model=? WHERE id=?", (model_name, video_id))
    _clear_previous(video_id)

    model = get_model(model_name)
    weather_cls = get_model(WEATHER_CLS_WEIGHTS.name) if WEATHER_CLS_WEIGHTS.exists() else None
    device = resolve_device()
    cal = store.get_calibration()

    cap = cv2.VideoCapture(str(video_file_path(video)))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 1
    step = max(1, round(fps / SAMPLE_FPS))
    tracker = HazardTracker(iou_threshold=0.3, max_gap_s=max(1.0, 3 * step / fps))
    hazard_rows: dict[int, int] = {}
    evidence_counts: dict[int, int] = {}

    idx = 0
    try:
        while cap.grab():
            if idx % step:
                idx += 1
                continue
            ok, frame = cap.retrieve()
            if not ok:
                break
            t = round(idx / fps, 3)
            height = frame.shape[0]
            result = model.predict(frame, conf=CONF_THRESHOLD, device=device, verbose=False)[0]
            dets = [
                {"box": [round(float(v), 1) for v in b.xyxy[0].tolist()],
                 "damage_type": model.names[int(b.cls)],
                 "confidence": round(float(b.conf), 3)}
                for b in result.boxes
            ]

            boxes_out = []
            for det, track, is_new in tracker.update(t, dets):
                dist = estimate_distance_m(det["box"][3], height, cal["camera_height_m"],
                                           cal["vfov_deg"], cal["horizon_frac"])
                if is_new:
                    tag = _weather_tag(frame, weather_cls)
                    hazard_rows[track.track_id] = store.execute(
                        "INSERT INTO hazards (video_id, track_id, damage_type, best_conf, first_t, last_t, "
                        "observations, distance_m, visibility_condition, ml_weather, ml_weather_conf) "
                        "VALUES (?,?,?,?,?,?,1,?,?,?,?)",
                        (video_id, track.track_id, det["damage_type"], det["confidence"], t, t, dist,
                         tag["visibility_condition"], tag["ml_weather"], tag["ml_weather_conf"]),
                    )
                    evidence_counts[track.track_id] = 0
                else:
                    best = det["confidence"] >= track.best_conf
                    store.execute(
                        "UPDATE hazards SET last_t=?, observations=?, best_conf=?, "
                        "distance_m=CASE WHEN ? THEN ? ELSE distance_m END WHERE id=?",
                        (t, track.observations, track.best_conf, best, dist, hazard_rows[track.track_id]),
                    )

                n_saved = evidence_counts[track.track_id]
                if n_saved < MAX_EVIDENCE_PER_HAZARD and (track.observations - 1) % EVIDENCE_EVERY == 0:
                    hid = hazard_rows[track.track_id]
                    fname = f"v{video_id}_h{hid}_{n_saved + 1:02d}.jpg"
                    cv2.imwrite(str(EVIDENCE_DIR / fname), _annotate(frame, det, track.track_id, dist),
                                [cv2.IMWRITE_JPEG_QUALITY, 85])
                    store.execute("INSERT INTO evidence (hazard_id, path, t, conf) VALUES (?,?,?,?)",
                                  (hid, fname, t, det["confidence"]))
                    evidence_counts[track.track_id] = n_saved + 1

                boxes_out.append({**det, "track_id": track.track_id, "distance_m": dist})

            if boxes_out:
                store.execute("INSERT INTO detections (video_id, t, boxes) VALUES (?,?,?)",
                              (video_id, t, json.dumps(boxes_out)))
            if (idx // step) % 10 == 0:
                store.execute("UPDATE videos SET progress=? WHERE id=?", (min(idx / total, 0.99), video_id))
            idx += 1
    finally:
        cap.release()

    store.execute(
        "UPDATE videos SET status='done', progress=1, analyzed_at=? WHERE id=?",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), video_id),
    )


def _worker() -> None:
    while True:
        video_id = _jobs.get()
        started = time.time()
        try:
            analyze(video_id)
            print(f"[replay] video {video_id} analyzed in {time.time() - started:.1f}s")
        except Exception as exc:
            traceback.print_exc()
            store.execute("UPDATE videos SET status='failed', error=? WHERE id=?", (str(exc)[:500], video_id))


def start_worker() -> None:
    threading.Thread(target=_worker, name="replay-worker", daemon=True).start()
