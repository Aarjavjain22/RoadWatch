"""Dataset inventory and model results for the Dataset & records page."""
from __future__ import annotations

import csv
import json
import os
import threading
import time

from app.config import EXTERNAL_DATASETS_DIR, RESULTS_DIR, ROOT

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
CACHE_TTL_S = 600

_cache: dict = {"at": 0.0, "data": None}
_lock = threading.Lock()


def _count_images(path) -> int:
    n = 0
    for _, _, files in os.walk(path):
        n += sum(1 for f in files if os.path.splitext(f)[1].lower() in IMAGE_EXTS)
    return n


def _dir_size_mb(path) -> float:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return round(total / 1e6, 1)


def _class_counts(split_dir) -> dict:
    if not split_dir.is_dir():
        return {}
    return {d.name: _count_images(d) for d in sorted(split_dir.iterdir()) if d.is_dir()}


def _scan() -> dict:
    external = []
    if EXTERNAL_DATASETS_DIR.is_dir():
        for d in sorted(EXTERNAL_DATASETS_DIR.iterdir()):
            if d.is_dir():
                external.append({"name": d.name, "images": _count_images(d), "size_mb": _dir_size_mb(d)})

    norway = ROOT / "data" / "rdd2022_norway" / "images"
    weather = ROOT / "data" / "weather_cls"
    prepared = {
        "rdd2022_norway": {s: _count_images(norway / s) for s in ("train", "val") if (norway / s).is_dir()},
        "weather_cls": {s: _class_counts(weather / s) for s in ("train", "val")},
    }
    return {"external": external, "prepared": prepared, "scanned_at": time.time()}


def inventory(force: bool = False) -> dict:
    with _lock:
        if force or _cache["data"] is None or time.time() - _cache["at"] > CACHE_TTL_S:
            _cache["data"], _cache["at"] = _scan(), time.time()
        return _cache["data"]


def model_results() -> dict:
    out = {}
    for p in sorted(RESULTS_DIR.glob("*.json")):
        try:
            out[p.stem] = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue

    cls_csv = ROOT / "runs" / "roadwatch" / "weather_cls" / "results.csv"
    if cls_csv.exists():
        with cls_csv.open() as f:
            rows = [{k.strip(): v for k, v in r.items()} for r in csv.DictReader(f)]
        if rows:
            best = max(rows, key=lambda r: float(r.get("metrics/accuracy_top1", 0)))
            out["weather_classifier"] = {
                "epochs_run": len(rows),
                "best_epoch": int(float(best["epoch"])),
                "top1_accuracy": round(float(best["metrics/accuracy_top1"]), 4),
                "top5_accuracy": round(float(best.get("metrics/accuracy_top5", 0)), 4),
            }
    return out
