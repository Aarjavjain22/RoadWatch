"""Replay image frames or sampled video frames into Kafka `raw_frames` with stable event identity.

`PRODUCER_SOURCE=images` (default) replays the JPG/PNG files in `FRAMES_DIR`.
`PRODUCER_SOURCE=video` samples `VIDEO_SAMPLE_FPS` frames per second from every
video in `VIDEO_DIR` (the Buffalo GoPro drives) and paces them in real time, so
the pipeline sees a dashcam-like stream of real Buffalo roads.
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import time
from collections import deque
from datetime import datetime, timezone

from kafka import KafkaProducer
from prometheus_client import Counter, Gauge, start_http_server

from contracts import video_frame_id, video_road_segment

EVENT_VERSION = "1.0"
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "localhost:29092")
FRAMES_DIR = os.getenv("FRAMES_DIR", "./data/frames")
METRICS_PORT = int(os.getenv("METRICS_PORT", "8001"))
FRAME_DELAY = float(os.getenv("FRAME_DELAY", "0.15"))
RAW_TOPIC = "raw_frames"

PRODUCER_SOURCE = os.getenv("PRODUCER_SOURCE", "images")
VIDEO_DIR = os.getenv("VIDEO_DIR", "./data/external_datasets/Road Damage GoPro Videos Buffalo")
VIDEO_SAMPLE_FPS = float(os.getenv("VIDEO_SAMPLE_FPS", "1.0"))
VIDEO_FRAME_DELAY = float(os.getenv("VIDEO_FRAME_DELAY", str(1.0 / VIDEO_SAMPLE_FPS)))
VIDEO_MAX_WIDTH = int(os.getenv("VIDEO_MAX_WIDTH", "1280"))
VIDEO_CAMERA_ID = os.getenv("VIDEO_CAMERA_ID", "gopro_buffalo")
VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv")
# Video frames have no file on disk, so the latest few are written here for the UI's Live monitor.
STREAM_FRAMES_DIR = os.getenv("STREAM_FRAMES_DIR", "")
STREAM_FRAMES_KEEP = int(os.getenv("STREAM_FRAMES_KEEP", "300"))

FRAMES_PRODUCED = Counter("frames_produced_total", "Frames published to Kafka")
PRODUCER_UP = Gauge("producer_up", "1 if the producer is running")


def road_segment_for(index: int, segments: int = 12) -> str:
    return f"segment_{(index % segments) + 1:02d}"


def iter_image_frames():
    images = sorted(glob.glob(os.path.join(FRAMES_DIR, "*.jpg")) + glob.glob(os.path.join(FRAMES_DIR, "*.png")))
    if not images:
        raise SystemExit(f"No images found in {FRAMES_DIR}")
    print(f"[producer] {len(images)} replay frames -> {RAW_TOPIC}")
    for path in images:
        with open(path, "rb") as f:
            yield {"source_image": os.path.basename(path)}, f.read()


def iter_video_frames():
    import cv2

    videos = sorted(p for p in glob.glob(os.path.join(VIDEO_DIR, "*")) if p.lower().endswith(VIDEO_EXTS))
    if not videos:
        raise SystemExit(f"No videos found in {VIDEO_DIR}")
    print(f"[producer] {len(videos)} videos @ {VIDEO_SAMPLE_FPS} fps -> {RAW_TOPIC}")
    for path in videos:
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            print(f"[producer][WARN] cannot open {path}")
            continue
        name = os.path.basename(path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        step = max(1, round(fps / VIDEO_SAMPLE_FPS))
        print(f"[producer] streaming {name}")
        index = 0
        try:
            while True:
                if index % step:
                    if not cap.grab():
                        break
                    index += 1
                    continue
                ok, frame = cap.read()
                if not ok:
                    break
                height, width = frame.shape[:2]
                if width > VIDEO_MAX_WIDTH:
                    frame = cv2.resize(frame, (VIDEO_MAX_WIDTH, round(height * VIDEO_MAX_WIDTH / width)),
                                       interpolation=cv2.INTER_AREA)
                ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                if ok:
                    offset_ms = round(index * 1000.0 / fps)
                    frame_id = video_frame_id(name, offset_ms)
                    yield {
                        "frame_id": frame_id,
                        "camera_id": VIDEO_CAMERA_ID,
                        "source_image": f"{frame_id}.jpg",
                        "source_video": name,
                        "video_offset_ms": offset_ms,
                        "road_segment": video_road_segment(name),
                    }, buf.tobytes()
                index += 1
        finally:
            cap.release()


class PreviewWriter:
    """Keeps the newest `keep` streamed frames on disk; older previews are deleted."""

    def __init__(self, directory: str, keep: int):
        self.directory = directory
        self.keep = keep
        self.recent: deque[str] = deque()
        if directory:
            os.makedirs(directory, exist_ok=True)
            for stale in glob.glob(os.path.join(directory, "*.jpg")):
                os.remove(stale)

    def write(self, name: str, data: bytes) -> None:
        if not self.directory:
            return
        path = os.path.join(self.directory, name)
        with open(path + ".tmp", "wb") as f:
            f.write(data)
        os.replace(path + ".tmp", path)
        if path in self.recent:
            self.recent.remove(path)
        self.recent.append(path)
        while len(self.recent) > self.keep:
            try:
                os.remove(self.recent.popleft())
            except OSError:
                pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--max-frames", type=int, default=0)
    args = parser.parse_args()

    start_http_server(METRICS_PORT)
    PRODUCER_UP.set(1)

    producer = KafkaProducer(
        bootstrap_servers=KAFKA_BOOTSTRAP,
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        key_serializer=lambda k: k.encode("utf-8"),
        acks="all",
        retries=5,
        enable_idempotence=True,
        max_request_size=5 * 1024 * 1024,
    )

    video_mode = PRODUCER_SOURCE == "video"
    frames = iter_video_frames if video_mode else iter_image_frames
    delay = VIDEO_FRAME_DELAY if video_mode else FRAME_DELAY
    previews = PreviewWriter(STREAM_FRAMES_DIR if video_mode else "", STREAM_FRAMES_KEEP)

    idx = 0
    while True:
        for meta, img_bytes in frames():
            if args.max_frames and idx >= args.max_frames:
                break
            frame_id = meta.pop("frame_id", f"frame_{idx:06d}")
            msg = {
                "event_version": EVENT_VERSION,
                "frame_id": frame_id,
                "trace_id": frame_id,
                "camera_id": "dashcam_001",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "road_segment": road_segment_for(idx),
                **meta,
                "image_b64": base64.b64encode(img_bytes).decode("utf-8"),
            }
            previews.write(msg["source_image"], img_bytes)
            producer.send(RAW_TOPIC, key=frame_id, value=msg).get(timeout=30)
            FRAMES_PRODUCED.inc()
            idx += 1
            time.sleep(delay)
        producer.flush()
        if not args.loop or (args.max_frames and idx >= args.max_frames):
            break


if __name__ == "__main__":
    main()
