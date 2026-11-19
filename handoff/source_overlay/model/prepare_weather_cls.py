"""Merge the downloaded weather-image datasets into one classification dataset.

Sources (under --root, as downloaded into data/external_datasets/):
  - "Weather-Time Classification with Road Images" (road scenes, train.json labels)
  - "Multi-class Weather Dataset" (MWD, one folder per class)
  - "Rome Weather" (one folder per class; the only source of fog/snow examples)

Every source label is mapped onto the five classes the Weather Agent reasons
about, then written as an Ultralytics/ImageFolder layout:
  <out_dir>/{train,val}/{clear,cloudy,fog,rain,snow}/*.jpg

Train with the existing trainer, e.g.
  python model/train.py --data data/weather_cls --model yolov8n-cls.pt --imgsz 224 --epochs 30
"""
from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from pathlib import Path

from PIL import Image

WEATHER_CLASSES = ["clear", "cloudy", "fog", "rain", "snow"]

ROAD_DATASET = "Weather-Time Classification with Road Images"
FOLDER_DATASETS = {
    "Multi-class Weather Dataset": {"Shine": "clear", "Sunrise": "clear", "Cloudy": "cloudy", "Rain": "rain"},
    "Rome Weather": {"Sunny": "clear", "Cloudy": "cloudy", "Foggy": "fog", "Rainy": "rain", "Snowy": "snow"},
}
ROAD_LABELS = {"Sunny": "clear", "Cloudy": "cloudy", "Rainy": "rain"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def collect(root: Path) -> list[tuple[Path, str, str]]:
    samples = []

    road_dir = root / ROAD_DATASET / "train_dataset"
    labels_file = road_dir / "train.json"
    if labels_file.exists():
        for ann in json.loads(labels_file.read_text())["annotations"]:
            label = ROAD_LABELS.get(ann["weather"])
            img = road_dir / ann["filename"].replace("\\", "/")
            if label and img.exists():
                samples.append((img, label, "road"))

    for dataset, mapping in FOLDER_DATASETS.items():
        for folder, label in mapping.items():
            for img in sorted((root / dataset / folder).glob("*")):
                if img.suffix.lower() in IMAGE_EXTS:
                    samples.append((img, label, dataset.split()[0].lower()))
    return samples


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="data/external_datasets")
    ap.add_argument("--out_dir", default="data/weather_cls")
    ap.add_argument("--val_frac", type=float, default=0.2)
    ap.add_argument("--max_side", type=int, default=512)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    samples = collect(Path(args.root))
    if not samples:
        raise SystemExit(f"no weather images found under {args.root}")

    rng = random.Random(args.seed)
    by_class: dict[str, list] = {c: [] for c in WEATHER_CLASSES}
    for sample in samples:
        by_class[sample[1]].append(sample)

    out = Path(args.out_dir)
    counts: Counter = Counter()
    skipped = 0
    for label, items in by_class.items():
        rng.shuffle(items)
        n_val = max(1, int(len(items) * args.val_frac)) if items else 0
        for i, (img_path, _, source) in enumerate(items):
            split = "val" if i < n_val else "train"
            dst_dir = out / split / label
            dst_dir.mkdir(parents=True, exist_ok=True)
            try:
                with Image.open(img_path) as im:
                    im = im.convert("RGB")
                    im.thumbnail((args.max_side, args.max_side))
                    im.save(dst_dir / f"{source}_{img_path.stem}.jpg", quality=90)
                counts[(split, label)] += 1
            except Exception as exc:
                skipped += 1
                print(f"[skip] {img_path}: {exc}")

    print(f"{'class':8s} {'train':>6s} {'val':>6s}")
    for label in WEATHER_CLASSES:
        print(f"{label:8s} {counts[('train', label)]:6d} {counts[('val', label)]:6d}")
    print(f"skipped {skipped} unreadable images; dataset written to {out.resolve()}")


if __name__ == "__main__":
    main()
