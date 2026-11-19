"""Add Buffalo GoPro frames to the weather-classifier training set.

The classifier trained by prepare_weather_cls.py has never seen this camera and
calls the rainy Buffalo clip "clear". This writes a new dataset: a copy of the
original one (--base, its validation split untouched) plus Buffalo frames in train/,
labelled from the clip file names ("Rainy" -> rain, otherwise --dry_class).

Footage is split so the result can be tested on frames the classifier never saw:
  - clips named in --holdout are not used at all;
  - a class with a single clip (rain) can only be split in time: its first
    --single_clip_train_frac is used, the rest stays unseen;
  - night clips are skipped: the agent ignores the classifier in low light.
The split is recorded in <out>/buffalo_split.json, which
`eval_weather_buffalo.py --exclude_json` reads to leave the training footage out.

The rainy clip is a letterboxed strip. With --strip_band, full frames are cropped to
a matching band (fractions of frame height, e.g. 0.34 0.73 = horizon and road, without
dashboard or wipers) so that rain cannot be learned from the shape of the frame.

Example:
  python model/prepare_weather_cls_buffalo.py --strip_band 0.34 0.73 \
      --holdout "Video 1 Morning.mp4" "Video 3 Morning.mp4" "Video 8 Morning.mp4"
  python model/train.py --data data/weather_cls_buffalo --model yolov8n-cls.pt --imgsz 224 \
      --epochs 30 --batch 32 --device 0 --workers 2 --patience 8 --name weather_cls_buffalo
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "streaming"))
sys.path.insert(0, str(ROOT / "model"))

from contracts import video_frame_id  # noqa: E402
from eval_weather_buffalo import VIDEO_EXTS, sample_frames  # noqa: E402

ADVERSE_KEYWORDS = ("rain", "snow", "fog")


def clip_class(video_name: str, dry_class: str = "clear") -> str | None:
    """Training class from the clip's file name; None for night clips, which are not used."""
    stem = Path(video_name).stem.lower()
    if "night" in stem:
        return None
    return next((k for k in ADVERSE_KEYWORDS if k in stem), dry_class)


def plan_split(durations: dict[str, float], holdout: set[str], single_clip_frac: float,
               dry_class: str = "clear") -> dict[str, dict]:
    """Which part of each clip trains the classifier: {clip: {"class", "start_s", "end_s"}}.

    A class with several clips must have at least one held out, so there is unseen
    footage to test on; a class with a single clip is split in time instead.
    """
    unknown = holdout - set(durations)
    if unknown:
        raise ValueError(f"held-out clips not found: {sorted(unknown)}")
    by_class: dict[str, list[str]] = {}
    for name in sorted(durations):
        cls = clip_class(name, dry_class)
        if cls:
            by_class.setdefault(cls, []).append(name)

    plan = {}
    for cls, names in by_class.items():
        train = [n for n in names if n not in holdout]
        if len(names) == 1:
            for n in train:
                plan[n] = {"class": cls, "start_s": 0.0, "end_s": float(round(durations[n] * single_clip_frac))}
        elif len(train) == len(names):
            raise ValueError(f"class '{cls}' has {len(names)} clips and none is held out for testing")
        else:
            for n in train:
                plan[n] = {"class": cls, "start_s": 0.0, "end_s": float(math.ceil(durations[n]))}
    return plan


def clip_duration(video: Path) -> float:
    import cv2

    cap = cv2.VideoCapture(str(video))
    try:
        return cap.get(cv2.CAP_PROP_FRAME_COUNT) / (cap.get(cv2.CAP_PROP_FPS) or 30.0)
    finally:
        cap.release()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=str(ROOT / "data/weather_cls"), help="dataset from prepare_weather_cls.py")
    ap.add_argument("--videos_dir", default=str(ROOT / "data/external_datasets/Road Damage GoPro Videos Buffalo"))
    ap.add_argument("--out", default=str(ROOT / "data/weather_cls_buffalo"))
    ap.add_argument("--holdout", nargs="+", required=True, help="clip file names kept unseen for testing")
    ap.add_argument("--single_clip_train_frac", type=float, default=0.6)
    ap.add_argument("--guard_sec", type=float, default=30.0,
                    help="gap the evaluation leaves around training footage (neighbouring frames are near-duplicates)")
    ap.add_argument("--strip_band", nargs=2, type=float, default=None, metavar=("TOP", "BOTTOM"))
    ap.add_argument("--dry_class", default="clear", choices=["clear", "cloudy"])
    ap.add_argument("--max_per_clip", type=int, default=200)
    ap.add_argument("--min_step_sec", type=float, default=1.0)
    ap.add_argument("--max_width", type=int, default=1280, help="match the producer's VIDEO_MAX_WIDTH")
    args = ap.parse_args()

    import cv2
    from image_heuristics import crop_letterbox

    base, out = Path(args.base), Path(args.out)
    if not (base / "train").is_dir() or not (base / "val").is_dir():
        raise SystemExit(f"{base} has no train/ and val/ — run model/prepare_weather_cls.py first")
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} is not empty — delete it or choose another --out")
    if args.strip_band and not 0.0 <= args.strip_band[0] < args.strip_band[1] <= 1.0:
        raise SystemExit("--strip_band needs 0 <= TOP < BOTTOM <= 1")

    videos = sorted(p for p in Path(args.videos_dir).iterdir() if p.suffix.lower() in VIDEO_EXTS)
    durations = {v.name: clip_duration(v) for v in videos}
    try:
        plan = plan_split(durations, set(args.holdout), args.single_clip_train_frac, args.dry_class)
    except ValueError as exc:
        raise SystemExit(str(exc))

    shutil.copytree(base, out, ignore=shutil.ignore_patterns("*.cache"), dirs_exist_ok=True)

    counts: Counter = Counter()
    for video in videos:
        entry = plan.get(video.name)
        if not entry:
            continue
        step = max(args.min_step_sec, (entry["end_s"] - entry["start_s"]) / args.max_per_clip)
        dst_dir = out / "train" / entry["class"]
        dst_dir.mkdir(parents=True, exist_ok=True)
        frames = 0
        for offset_ms, frame in sample_frames(video, step, args.max_width, entry["start_s"], entry["end_s"]):
            scene = crop_letterbox(frame)
            if args.strip_band and scene.shape == frame.shape:
                height = frame.shape[0]
                scene = frame[round(args.strip_band[0] * height):round(args.strip_band[1] * height)]
            name = f"buffalo_{video_frame_id(video.name, offset_ms)}.jpg"
            cv2.imwrite(str(dst_dir / name), scene, [cv2.IMWRITE_JPEG_QUALITY, 90])
            frames += 1
        entry["frames"] = frames
        counts[entry["class"]] += frames
        print(f"{video.name:40s} {entry['class']:6s} {entry['start_s']:6.0f}-{entry['end_s']:<6.0f} s {frames:5d} frames")

    split = {
        "base": str(base), "videos_dir": str(args.videos_dir), "holdout": sorted(args.holdout),
        "guard_sec": args.guard_sec, "strip_band": args.strip_band, "max_width": args.max_width,
        "train": plan, "not_used": sorted(set(durations) - set(plan)),
    }
    (out / "buffalo_split.json").write_text(json.dumps(split, indent=2), encoding="utf-8")
    print(f"\nBuffalo frames added to train/: {dict(counts)}")
    print(f"dataset: {out}\nsplit:   {out / 'buffalo_split.json'}")


if __name__ == "__main__":
    main()
