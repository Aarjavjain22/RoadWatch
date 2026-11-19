"""Manual tool: pull sample traffic-camera snapshots for Buffalo's I-190 corridor
from the 511NY Developer API, for geographic sanity-checking of the Weather &
Context-Enrichment Agent's heuristic against real local footage.

This is intentionally NOT part of the running pipeline (not in docker-compose,
not a Kafka consumer) — it's a one-off/periodic script a developer runs by hand
to build a small local "data/buffalo_samples/" set, per the dataset-diversity
plan's "validate geographically" step.

Usage:
    cd streaming
    python tools/buffalo_camera_sampler.py --out ../data/buffalo_samples --limit 10

Requires NY511_API_KEY (see .env.example). Register at https://511ny.org/developers/
or https://511ny.org/help ("For Developers").

KNOWN RISK: as of late 2026, 511NY has been migrating to a new "511x" platform
(Castle Rock CARS). The legacy `getcameras` endpoint documented here may return
HTML instead of JSON if your account/key predates the cutover, or may require a
new Developer Access Agreement. This script detects that failure mode and
prints a clear diagnostic instead of silently producing garbage output.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests

NY511_API_URL = "https://511ny.org/api/getcameras"
BUFFALO_KEYWORDS = ("I-190", "190", "BUFFALO", "NIAGARA", "190 ")


def fetch_cameras(api_key: str) -> list[dict]:
    resp = requests.get(NY511_API_URL, params={"key": api_key, "format": "json"}, timeout=15)
    resp.raise_for_status()
    try:
        return resp.json()
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "511NY did not return JSON (got "
            f"content-type={resp.headers.get('content-type')!r}). This usually means "
            "the legacy getcameras API has been retired by the 511x platform cutover, "
            "or your key needs a new Developer Access Agreement. See https://511ny.org/help "
            "('For Developers') for the current process."
        ) from exc


def filter_buffalo_cameras(cameras: list[dict]) -> list[dict]:
    def is_buffalo(cam: dict) -> bool:
        haystack = " ".join(str(cam.get(field, "")) for field in ("Name", "RoadwayName")).upper()
        return any(kw in haystack for kw in BUFFALO_KEYWORDS)

    return [cam for cam in cameras if is_buffalo(cam)]


def download_snapshot(cam: dict, out_dir: Path, index: int) -> Path | None:
    image_url = cam.get("Url") or cam.get("ImageUrl")
    if not image_url:
        return None
    try:
        img = requests.get(image_url, timeout=10)
        img.raise_for_status()
    except Exception as exc:
        print(f"[camera-sampler][WARN] could not fetch {image_url}: {exc}")
        return None

    name = (cam.get("Name") or f"camera_{index}").replace("/", "-").replace(" ", "_")
    path = out_dir / f"{index:03d}_{name}.jpg"
    path.write_bytes(img.content)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="../data/buffalo_samples", help="Output directory for snapshots")
    parser.add_argument("--limit", type=int, default=10, help="Max number of camera snapshots to download")
    args = parser.parse_args()

    api_key = os.getenv("NY511_API_KEY", "").strip()
    if not api_key:
        print(
            "[camera-sampler] NY511_API_KEY is not set. Register at https://511ny.org/help "
            "('For Developers') and set it in .env, then re-run this script.",
            file=sys.stderr,
        )
        sys.exit(1)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("[camera-sampler] fetching statewide camera list from 511NY...")
    try:
        cameras = fetch_cameras(api_key)
    except Exception as exc:
        print(f"[camera-sampler][ERROR] {exc}", file=sys.stderr)
        sys.exit(1)

    buffalo_cameras = filter_buffalo_cameras(cameras)[: args.limit]
    if not buffalo_cameras:
        print("[camera-sampler] no Buffalo/I-190 cameras matched — the statewide list may have "
              "changed field names after the 511x migration; inspect one raw record manually.")
        return

    print(f"[camera-sampler] found {len(buffalo_cameras)} Buffalo-area cameras, downloading snapshots...")
    saved = 0
    for i, cam in enumerate(buffalo_cameras):
        path = download_snapshot(cam, out_dir, i)
        if path:
            saved += 1
            print(f"[camera-sampler] saved {path}")

    print(f"[camera-sampler] done: {saved}/{len(buffalo_cameras)} snapshots saved to {out_dir}")


if __name__ == "__main__":
    main()
