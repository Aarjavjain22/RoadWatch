"""Compare the road-damage detector's output across the Buffalo GoPro conditions.

The drives have no damage annotations, so this cannot measure accuracy. It measures
how the detector's output changes between dry daytime, rain and night — how often it
fires, how confident it is, which classes it reports — and how much the Weather
Agent's verdict for the same frame discounts those detections (the
`detection_weather` view: confidence x confidence_adjustment).

Inputs: frames written by `eval_weather_buffalo.py --export_frames` and that script's
per-frame predictions CSV (joined on frame_id). Detection mirrors `yolo_consumer.py`.

The only rainy clip is a letterboxed strip that shows less road than a full frame.
With --strip_band, dry frames are also run reduced to the same kind of strip
("dry_day_strip_view"), which is the like-for-like comparison for rain.

Example:
  python model/eval_detection_buffalo.py --strip_band 0.34 0.73
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import statistics
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "streaming"))
sys.path.insert(0, str(ROOT / "model"))

from contracts import CLASS_NAMES, severity_from_area  # noqa: E402
from eval_weather_buffalo import clip_labels  # noqa: E402

GROUPS = ("dry_day", "dry_day_strip_view", "rain", "night")
CONFIDENT = 0.5


def condition_group(video_name: str) -> str:
    labels = clip_labels(video_name)
    return "night" if labels["night"] else "rain" if labels["rain"] else "dry_day"


def strip_view(frame, band):
    """Mimic the letterboxed rainy clip: keep only the `band` rows, at the bottom of a black frame."""
    import numpy as np

    height = frame.shape[0]
    strip = frame[round(band[0] * height):round(band[1] * height)]
    out = np.zeros_like(frame)
    out[height - strip.shape[0]:] = strip
    return out


def detect(model, image, conf: float, device: str | None = None) -> list[dict]:
    height, width = image.shape[:2]
    detections = []
    for box in model.predict(image, conf=conf, verbose=False, **({"device": device} if device else {}))[0].boxes:
        class_id, confidence = int(box.cls[0]), float(box.conf[0])
        x1, y1, x2, y2 = (float(v) for v in box.xyxy[0])
        area_ratio = (x2 - x1) * (y2 - y1) / float(width * height)
        detections.append({
            "damage_type": CLASS_NAMES[class_id] if class_id < len(CLASS_NAMES) else str(class_id),
            "confidence": round(confidence, 4),
            "x1": round(x1, 1), "y1": round(y1, 1), "x2": round(x2, 1), "y2": round(y2, 1),
            "bbox_area_ratio": round(area_ratio, 6),
            "severity": severity_from_area(area_ratio, confidence),
        })
    return detections


def summarize(frames: int, detections: list[dict]) -> dict:
    """Detector output for `frames` frames; rates are per frame so groups of different size compare."""
    confidences = [d["confidence"] for d in detections]
    out = {
        "frames": frames,
        "frames_with_detection_pct": round(100.0 * len({d["frame_id"] for d in detections}) / frames, 1),
        "detections": len(detections),
        "detections_per_100_frames": round(100.0 * len(detections) / frames, 1),
        "confident_detections_per_100_frames": round(100.0 * sum(c >= CONFIDENT for c in confidences) / frames, 1),
        "median_confidence": round(statistics.median(confidences), 3) if confidences else None,
        "by_class": dict(Counter(d["damage_type"] for d in detections).most_common()),
        "by_severity": dict(Counter(d["severity"] for d in detections).most_common()),
    }
    adjusted = [d for d in detections if d.get("confidence_adjustment") is not None]
    if adjusted:
        out["mean_confidence_adjustment"] = round(statistics.mean(d["confidence_adjustment"] for d in adjusted), 3)
        out["median_adjusted_confidence"] = round(statistics.median(d["adjusted_confidence"] for d in adjusted), 3)
        out["on_escalated_frames_pct"] = round(100.0 * sum(d["escalation_flag"] for d in adjusted) / len(adjusted), 1)
    return out


def contact_sheet(detections: list[dict], frames_dir: Path, band, path: Path, count: int = 12, seed: int = 0) -> None:
    """Save a grid of randomly chosen detections, each cropped with some road around the box."""
    import cv2
    import numpy as np

    tile_w, tile_h, cols = 400, 300, 4
    picks = random.Random(seed).sample(detections, min(count, len(detections)))
    tiles = []
    for d in picks:
        frame = cv2.imread(str(frames_dir / f"{d['frame_id']}.jpg"))
        if d["group"] == "dry_day_strip_view":
            frame = strip_view(frame, band)
        height, width = frame.shape[:2]
        box_w, box_h = d["x2"] - d["x1"], d["y2"] - d["y1"]
        win_w = min(max(2.2 * box_w, 2.2 * box_h * tile_w / tile_h, tile_w), height * tile_w / tile_h)
        win_h = win_w * tile_h / tile_w
        x0 = int(min(max((d["x1"] + d["x2"]) / 2 - win_w / 2, 0), width - win_w))
        y0 = int(min(max((d["y1"] + d["y2"]) / 2 - win_h / 2, 0), height - win_h))
        scale = tile_w / win_w
        tile = cv2.resize(frame[y0:y0 + int(win_h), x0:x0 + int(win_w)], (tile_w, tile_h), interpolation=cv2.INTER_AREA)
        corners = [int((v - o) * scale) for v, o in ((d["x1"], x0), (d["y1"], y0), (d["x2"], x0), (d["y2"], y0))]
        cv2.rectangle(tile, corners[:2], corners[2:], (0, 255, 255), 2)
        for colour, thickness in (((0, 0, 0), 3), ((255, 255, 255), 1)):
            cv2.putText(tile, f"{d['damage_type']} {d['confidence']:.2f}", (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                        colour, thickness, cv2.LINE_AA)
            cv2.putText(tile, d["frame_id"], (8, tile_h - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.4, colour, thickness, cv2.LINE_AA)
        tiles.append(tile)
    tiles += [np.zeros((tile_h, tile_w, 3), np.uint8)] * (-len(tiles) % cols)
    rows = [np.hstack(tiles[i:i + cols]) for i in range(0, len(tiles), cols)]
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), np.vstack(rows), [cv2.IMWRITE_JPEG_QUALITY, 85])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames_dir", default=str(ROOT / "data/buffalo_eval_frames"))
    ap.add_argument("--weather_csv", default=str(ROOT / "results/buffalo_weather_predictions.csv"))
    ap.add_argument("--weights", default=str(ROOT / "model/weights/best.pt"))
    ap.add_argument("--conf", type=float, default=0.25, help="match the pipeline's CONF_THRES")
    ap.add_argument("--device", default=None)
    ap.add_argument("--strip_band", nargs=2, type=float, default=None, metavar=("TOP", "BOTTOM"))
    ap.add_argument("--out_json", default=str(ROOT / "results/buffalo_detection_by_condition.json"))
    ap.add_argument("--out_csv", default=str(ROOT / "results/buffalo_detections.csv"))
    ap.add_argument("--samples_dir", default=str(ROOT / "results/buffalo_detection_samples"),
                    help="contact sheets of random detections per condition; '' to skip")
    args = ap.parse_args()

    import cv2
    from ultralytics import YOLO

    frames_dir = Path(args.frames_dir)
    with Path(args.weather_csv).open(newline="", encoding="utf-8") as f:
        weather_rows = [r for r in csv.DictReader(f) if (frames_dir / f"{r['frame_id']}.jpg").exists()]
    if not weather_rows:
        raise SystemExit(f"no frame listed in {args.weather_csv} exists in {frames_dir}")

    model = YOLO(args.weights)
    frames: Counter = Counter()
    frames_by_clip: Counter = Counter()
    detections: list[dict] = []
    for row in weather_rows:
        frame = cv2.imread(str(frames_dir / f"{row['frame_id']}.jpg"))
        group = condition_group(row["video"])
        adjustment = float(row["confidence_adjustment"])
        views = [(group, frame, True)]
        if args.strip_band and group == "dry_day":
            views.append(("dry_day_strip_view", strip_view(frame, args.strip_band), False))
        for view_group, image, with_weather in views:
            frames[view_group] += 1
            frames_by_clip[(view_group, row["video"])] += 1
            for d in detect(model, image, args.conf, args.device):
                d = {"frame_id": row["frame_id"], "video": row["video"], "group": view_group, **d}
                if with_weather:
                    d.update(visibility_condition=row["visibility_condition"], confidence_adjustment=adjustment,
                             adjusted_confidence=round(d["confidence"] * adjustment, 4),
                             escalation_flag=row["escalation_flag"] == "True")
                detections.append(d)

    by_group = {g: [d for d in detections if d["group"] == g] for g in GROUPS if frames[g]}
    report = {
        "note": "no damage annotations exist for these drives: this describes detector output, not accuracy",
        "weights": args.weights, "conf_threshold": args.conf, "confident_threshold": CONFIDENT,
        "weather_csv": args.weather_csv, "strip_band": args.strip_band,
        "by_condition": {g: summarize(frames[g], dets) for g, dets in by_group.items()},
        "by_clip": {
            f"{video} [{g}]": summarize(n, [d for d in by_group[g] if d["video"] == video])
            for (g, video), n in sorted(frames_by_clip.items(), key=lambda item: (item[0][1], item[0][0]))
        },
    }

    out_json = Path(args.out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    fields = ["frame_id", "video", "group", "damage_type", "confidence", "x1", "y1", "x2", "y2", "bbox_area_ratio",
              "severity", "visibility_condition", "confidence_adjustment", "adjusted_confidence", "escalation_flag"]
    with Path(args.out_csv).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, restval="")
        writer.writeheader()
        writer.writerows(detections)
    if args.samples_dir:
        for g, dets in by_group.items():
            if dets:
                contact_sheet(dets, frames_dir, args.strip_band, Path(args.samples_dir) / f"{g}.jpg")

    print(f"{'condition':20s} {'frames':>6s} {'with det':>9s} {'det/100':>8s} {'conf>=.5/100':>13s} {'median conf':>12s} "
          f"{'weather adj':>12s}")
    for g, s in report["by_condition"].items():
        median = f"{s['median_confidence']:.3f}" if s["median_confidence"] is not None else "-"
        adj = f"{s['mean_confidence_adjustment']:.3f}" if "mean_confidence_adjustment" in s else "-"
        print(f"{g:20s} {s['frames']:6d} {s['frames_with_detection_pct']:8.1f}% {s['detections_per_100_frames']:8.1f} "
              f"{s['confident_detections_per_100_frames']:13.1f} {median:>12s} {adj:>12s}")
    print(f"\nreport: {out_json}\ndetections: {args.out_csv}")


if __name__ == "__main__":
    main()
