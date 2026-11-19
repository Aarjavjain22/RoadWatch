"""Evaluate the Weather Agent's image-based signals on the Buffalo GoPro drives.

The clips were re-exported (Clipchamp) and carry no recording time or GPS, so the
Open-Meteo reading cannot be matched to them. This scores the two signals that look
at the frame itself — pixel heuristics and the weather classifier — exactly as
`streaming/weather_agent.py` combines them, with the external weather left "unknown".

Labels come from either:
  - the clip file names (default): "Night" -> night, "Rainy" -> rain; every frame
    inherits its clip's label, so this is a coarse, clip-level check;
  - a reviewed labeling sheet (--labels), written by --export_frames with the clip
    labels pre-filled for correction frame by frame.

Tasks scored per frame:
  night     label: night clip / label_night     prediction: "low_light" reason
  rain      label: rainy clip / label_rain      prediction: "ml_rain" reason (classifier >= ML_CONFIDENCE_MIN)
  degraded  label: night or rain                prediction: visibility_condition != "clear"

Frames are read from the videos (slow: every frame is a seek) or, with --frames_dir,
from frames already written by --export_frames (seconds instead of minutes).

A classifier trained on Buffalo footage must be scored on footage it has not seen:
--exclude_json takes the split written by model/prepare_weather_cls_buffalo.py and
leaves out everything used for training. --strip_control also crops full frames to
the shape of the letterboxed rainy clip, to show the classifier does not key on frame shape.

Example:
  python model/eval_weather_buffalo.py --export_frames
  python model/eval_weather_buffalo.py --labels results/buffalo_frame_labels.csv \
                                       --out_json results/buffalo_weather_eval_labeled.json
  python model/eval_weather_buffalo.py --frames_dir data/buffalo_eval_frames \
                                       --exclude_json data/weather_cls_buffalo/buffalo_split.json
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "streaming"))

from contracts import classify_visibility, video_frame_id, video_slug  # noqa: E402

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv"}
TASKS = ("night", "rain", "degraded")


def clip_labels(video_name: str) -> dict[str, bool]:
    stem = Path(video_name).stem.lower()
    night, rain = "night" in stem, "rain" in stem
    return {"night": night, "rain": rain, "degraded": night or rain}


def predicted_labels(condition: str) -> dict[str, bool]:
    reasons = set(condition.split("+"))
    return {"night": "low_light" in reasons, "rain": "ml_rain" in reasons, "degraded": condition != "clear"}


def binary_metrics(pairs: list[tuple[bool, bool]]) -> dict:
    """`pairs` are (label, prediction); undefined ratios are reported as None, not 0."""
    tp = sum(1 for y, p in pairs if y and p)
    fp = sum(1 for y, p in pairs if not y and p)
    fn = sum(1 for y, p in pairs if y and not p)
    tn = sum(1 for y, p in pairs if not y and not p)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = 2 * precision * recall / (precision + recall) if precision and recall else None
    return {
        "n": len(pairs), "positives": tp + fn, "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": precision, "recall": recall, "f1": f1,
        "accuracy": (tp + tn) / len(pairs) if pairs else None,
    }


def _truthy(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "x"}


def load_sheet(path: Path) -> dict[str, dict[str, bool]]:
    labels = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            night, rain = _truthy(row["label_night"]), _truthy(row["label_rain"])
            labels[row["frame_id"]] = {"night": night, "rain": rain, "degraded": night or rain}
    return labels


def used_in_training(split: dict, video_name: str, offset_s: float) -> bool:
    """True if this clip position trained the classifier, or lies within the guard gap around
    that range (neighbouring frames of a drive are near-duplicates). Clips are matched by
    slug, so both the file name and the `<slug>` prefix of an exported frame work."""
    slug, guard = video_slug(video_name), split.get("guard_sec", 0)
    return any(
        video_slug(name) == slug and entry["start_s"] - guard <= offset_s < entry["end_s"] + guard
        for name, entry in split["train"].items()
    )


def default_predictions_path(out_json: Path) -> Path:
    """`buffalo_weather_eval_x.json` -> `buffalo_weather_predictions_x.csv`, so each report keeps its own predictions."""
    stem = out_json.stem
    stem = stem.replace("_eval", "_predictions", 1) if "_eval" in stem else f"{stem}_predictions"
    return out_json.with_name(f"{stem}.csv")


def sample_frames(video: Path, every_sec: float, max_width: int, start_s: float = 0.0,
                  end_s: float | None = None, skip=None):
    """Yield (offset_ms, frame) every `every_sec`; positions where `skip(offset_ms)` is true are not decoded."""
    import cv2

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        print(f"[skip] cannot open {video.name}")
        return
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    end_ms = cap.get(cv2.CAP_PROP_FRAME_COUNT) / fps * 1000.0
    if end_s is not None:
        end_ms = min(end_ms, end_s * 1000.0)
    try:
        for i in itertools.count():
            offset_ms = round((start_s + i * every_sec) * 1000)
            if offset_ms >= end_ms:
                break
            if skip and skip(offset_ms):
                continue
            cap.set(cv2.CAP_PROP_POS_MSEC, offset_ms)
            ok, frame = cap.read()
            if not ok:
                break
            height, width = frame.shape[:2]
            if width > max_width:
                frame = cv2.resize(frame, (max_width, round(height * max_width / width)), interpolation=cv2.INTER_AREA)
            yield offset_ms, frame
    finally:
        cap.release()


def exported_frames(frames_dir: Path, videos: list[Path], skip):
    """Yield (video_name, offset_ms, frame) for `<clip slug>_<offset ms>.jpg` files written by --export_frames."""
    import cv2

    names = {video_slug(v.name): v.name for v in videos}
    for path in sorted(frames_dir.glob("*.jpg")):
        slug, _, offset = path.stem.rpartition("_")
        if not slug or not offset.isdigit():
            continue
        name, offset_ms = names.get(slug, slug), int(offset)
        if skip(name, offset_ms):
            continue
        frame = cv2.imread(str(path))
        if frame is not None:
            yield name, offset_ms, frame


def summarize_clip(rows: list[dict]) -> dict:
    n = len(rows)
    mean = lambda key: round(sum(r[key] for r in rows) / n, 2)  # noqa: E731
    pct = lambda count: round(100.0 * count / n, 1)  # noqa: E731
    return {
        "frames": n,
        "clip_labels": {k: v for k, v in clip_labels(rows[0]["video"]).items() if k != "degraded"},
        "mean_brightness": mean("brightness"),
        "mean_contrast": mean("contrast"),
        "mean_blur_score": mean("blur_score"),
        "mean_glare_ratio": round(sum(r["glare_ratio"] for r in rows) / n, 4),
        "low_light_pct": pct(sum("low_light" in r["visibility_condition"].split("+") for r in rows)),
        "escalated_pct": pct(sum(r["escalation_flag"] for r in rows)),
        "mean_confidence_adjustment": round(sum(r["confidence_adjustment"] for r in rows) / n, 3),
        "ml_weather_pct": {k: pct(v) for k, v in Counter(r["ml_weather_class"] for r in rows).most_common()},
        "top_conditions": dict(Counter(r["visibility_condition"] for r in rows).most_common(5)),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--videos_dir", default=str(ROOT / "data/external_datasets/Road Damage GoPro Videos Buffalo"))
    ap.add_argument("--frames_dir", default=None, help="read frames written by --export_frames instead of decoding the videos")
    ap.add_argument("--only", nargs="+", default=None, help="only clips whose file name contains one of these (case-insensitive)")
    ap.add_argument("--weights", default=str(ROOT / "model/weights/weather_cls.pt"))
    ap.add_argument("--every_sec", type=float, default=5.0)
    ap.add_argument("--max_width", type=int, default=1280, help="match the producer's VIDEO_MAX_WIDTH; blur depends on it")
    ap.add_argument("--device", default=None)
    ap.add_argument("--no_letterbox_crop", action="store_true", help="skip crop_letterbox, as the agent did before it")
    ap.add_argument("--exclude_json", default=None, help="split from prepare_weather_cls_buffalo.py; training footage is left out")
    ap.add_argument("--strip_control", action="store_true",
                    help="crop full frames to the strip band recorded in --exclude_json (shape control)")
    ap.add_argument("--labels", default=None, help="reviewed labeling sheet; default uses clip file names")
    ap.add_argument("--export_frames", action="store_true", help="save frames + a labeling sheet for manual review")
    ap.add_argument("--frames_out", default=str(ROOT / "data/buffalo_eval_frames"))
    ap.add_argument("--sheet_out", default=str(ROOT / "results/buffalo_frame_labels.csv"))
    ap.add_argument("--out_json", default=str(ROOT / "results/buffalo_weather_eval.json"))
    ap.add_argument("--out_csv", default=None, help="per-frame predictions; default is named after --out_json")
    args = ap.parse_args()

    import cv2
    from image_heuristics import compute_image_heuristics, crop_letterbox
    from ultralytics import YOLO

    sheet_path = Path(args.sheet_out)
    if args.export_frames and args.frames_dir:
        raise SystemExit("--export_frames reads the videos; do not combine it with --frames_dir")
    if args.export_frames and sheet_path.exists():
        raise SystemExit(f"{sheet_path} already exists — move it first so reviewed labels are not overwritten")
    if args.strip_control and (not args.exclude_json or args.no_letterbox_crop):
        raise SystemExit("--strip_control needs --exclude_json (the strip band is recorded there) and the letterbox crop")
    sheet_labels = load_sheet(Path(args.labels)) if args.labels else None
    split = json.loads(Path(args.exclude_json).read_text(encoding="utf-8")) if args.exclude_json else None
    band = split["strip_band"] if args.strip_control else None
    if args.strip_control and not band:
        raise SystemExit(f"{args.exclude_json} records no strip band — the training frames were not cropped")
    only = [s.lower() for s in args.only] if args.only else None

    def wanted(video_name: str) -> bool:
        return not only or any(s in video_name.lower() for s in only)

    def skip(video_name: str, offset_ms: int) -> bool:
        if not wanted(video_name):
            return True
        if split and used_in_training(split, video_name, offset_ms / 1000):
            return True
        return sheet_labels is not None and video_frame_id(video_name, offset_ms) not in sheet_labels

    def video_frames(videos: list[Path]):
        for video in filter(lambda v: wanted(v.name), videos):
            for offset_ms, frame in sample_frames(video, args.every_sec, args.max_width,
                                                  skip=lambda ms: skip(video.name, ms)):
                yield video.name, offset_ms, frame

    videos_dir = Path(args.videos_dir)
    videos = sorted(p for p in videos_dir.iterdir() if p.suffix.lower() in VIDEO_EXTS) if videos_dir.is_dir() else []
    frames = exported_frames(Path(args.frames_dir), videos, skip) if args.frames_dir else video_frames(videos)

    classifier = YOLO(args.weights)
    predict_kwargs = {"device": args.device} if args.device else {}
    frames_out = Path(args.frames_out)
    if args.export_frames:
        frames_out.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    for video_name, offset_ms, frame in frames:
        frame_id = video_frame_id(video_name, offset_ms)
        scene = frame if args.no_letterbox_crop else crop_letterbox(frame)
        if band and scene.shape == frame.shape:
            height = frame.shape[0]
            scene = frame[round(band[0] * height):round(band[1] * height)]
        heur = compute_image_heuristics(scene)
        probs = classifier.predict(scene, verbose=False, **predict_kwargs)[0].probs
        ml_class, ml_conf = classifier.names[int(probs.top1)], round(float(probs.top1conf), 4)
        condition, adjustment, escalate = classify_visibility(
            heur["brightness"], heur["contrast"], heur["blur_score"], heur["glare_ratio"],
            "unknown", ml_class, ml_conf,
        )
        labels = sheet_labels[frame_id] if sheet_labels is not None else clip_labels(video_name)
        row = {
            "frame_id": frame_id, "video": video_name, "offset_s": offset_ms / 1000, **heur,
            "ml_weather_class": ml_class, "ml_weather_confidence": ml_conf,
            "visibility_condition": condition, "confidence_adjustment": adjustment, "escalation_flag": escalate,
            **{f"label_{k}": v for k, v in labels.items()},
            **{f"pred_{k}": v for k, v in predicted_labels(condition).items()},
        }
        if args.export_frames:
            row["image_path"] = str((frames_out / f"{frame_id}.jpg").relative_to(ROOT))
            cv2.imwrite(str(frames_out / f"{frame_id}.jpg"), frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        rows.append(row)

    if not rows:
        raise SystemExit("no frames evaluated")

    by_video: dict[str, list[dict]] = {}
    for r in rows:
        by_video.setdefault(r["video"], []).append(r)
    for video_name, clip_rows in by_video.items():
        print(f"{video_name:40s} {len(clip_rows):5d} frames")

    report = {
        "label_source": f"sheet:{args.labels}" if args.labels else "clip file names",
        "external_weather": "not used — clips have no recording timestamp or GPS",
        "letterbox_crop": not args.no_letterbox_crop,
        "frame_source": f"exported frames: {args.frames_dir}" if args.frames_dir else f"videos, every {args.every_sec} s",
        "unseen_footage_only": bool(split),
        "strip_control": bool(band),
        "max_width": None if args.frames_dir else args.max_width,
        "weights": args.weights,
        "frames": len(rows),
        "metrics": {t: binary_metrics([(r[f"label_{t}"], r[f"pred_{t}"]) for r in rows]) for t in TASKS},
        "per_clip": {v: summarize_clip(rs) for v, rs in by_video.items()},
    }

    out_json = Path(args.out_json)
    out_csv = Path(args.out_csv) if args.out_csv else default_predictions_path(out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    if args.export_frames:
        with sheet_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["frame_id", "video", "offset_s", "image_path", "label_night", "label_rain", "notes"])
            for r in rows:
                writer.writerow([r["frame_id"], r["video"], r["offset_s"], r["image_path"],
                                 int(r["label_night"]), int(r["label_rain"]), ""])
        print(f"labeling sheet: {sheet_path} (labels pre-filled from clip names — correct per frame)")

    print(f"\n{'task':10s} {'n':>6s} {'pos':>6s} {'prec':>7s} {'recall':>7s} {'f1':>7s} {'acc':>7s}")
    fmt = lambda v: f"{v:7.3f}" if v is not None else f"{'-':>7s}"  # noqa: E731
    for task, m in report["metrics"].items():
        print(f"{task:10s} {m['n']:6d} {m['positives']:6d} {fmt(m['precision'])} {fmt(m['recall'])} {fmt(m['f1'])} {fmt(m['accuracy'])}")
    print(f"\nreport: {out_json}\npredictions: {out_csv}")


if __name__ == "__main__":
    main()
