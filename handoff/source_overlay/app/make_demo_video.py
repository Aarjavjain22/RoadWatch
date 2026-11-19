"""Build a demo road video from labeled still images, for exercising the Detection replay page.

Each image is held for a few seconds with a slow zoom (like a car rolling forward),
so the hazard tracker sees motion instead of a frozen frame.

    python -m app.make_demo_video --images data/rdd2022_norway/images/val --count 12
"""
from __future__ import annotations

import argparse
import random
from pathlib import Path

import cv2
import imageio_ffmpeg
import numpy as np

from app.config import VIDEOS_DIR

W, H = 1280, 720


def fit(img: np.ndarray) -> np.ndarray:
    """Center-crop to 16:9 and resize to W x H."""
    h, w = img.shape[:2]
    target = W / H
    if w / h > target:
        nw = int(h * target)
        img = img[:, (w - nw) // 2:(w - nw) // 2 + nw]
    else:
        nh = int(w / target)
        img = img[(h - nh) // 2:(h - nh) // 2 + nh]
    return cv2.resize(img, (W, H), interpolation=cv2.INTER_AREA)


def zoom(img: np.ndarray, factor: float) -> np.ndarray:
    """Zoom toward the lower-center of the frame, where the road is."""
    cw, ch = int(W / factor), int(H / factor)
    x0, y0 = (W - cw) // 2, H - ch
    return cv2.resize(img[y0:y0 + ch, x0:x0 + cw], (W, H), interpolation=cv2.INTER_LINEAR)


def labeled_images(images_dir: Path) -> list[Path]:
    labels_dir = images_dir.parent.parent / "labels" / images_dir.name
    out = []
    for p in sorted(images_dir.glob("*.jpg")):
        lbl = labels_dir / f"{p.stem}.txt"
        if lbl.exists() and lbl.stat().st_size > 0:
            out.append(p)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", default="data/rdd2022_norway/images/val")
    ap.add_argument("--count", type=int, default=12)
    ap.add_argument("--seconds", type=float, default=2.5, help="seconds per image")
    ap.add_argument("--fps", type=int, default=20)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=str(VIDEOS_DIR / "norway_demo.mp4"))
    args = ap.parse_args()

    images = labeled_images(Path(args.images))
    if not images:
        raise SystemExit(f"no labeled images under {args.images}")
    random.Random(args.seed).shuffle(images)
    images = images[: args.count]

    frames_per_image = int(args.seconds * args.fps)
    writer = imageio_ffmpeg.write_frames(
        args.out, (W, H), fps=args.fps, codec="libx264", pix_fmt_in="rgb24", pix_fmt_out="yuv420p",
        output_params=["-movflags", "+faststart", "-crf", "23"],
    )
    writer.send(None)
    for p in images:
        base = fit(cv2.imread(str(p)))
        for i in range(frames_per_image):
            frame = zoom(base, 1.0 + 0.18 * i / frames_per_image)
            writer.send(np.ascontiguousarray(frame[:, :, ::-1]))
    writer.close()
    print(f"[demo] wrote {args.out} ({len(images)} images, {len(images) * args.seconds:.0f}s)")


if __name__ == "__main__":
    main()
