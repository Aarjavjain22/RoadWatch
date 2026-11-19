"""Dependency-free geometry for the replay UI: box overlap, hazard tracking, distance."""
from __future__ import annotations

import math
from dataclasses import dataclass


def iou(a: list[float], b: list[float]) -> float:
    """Intersection-over-union of two [x1, y1, x2, y2] boxes."""
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    if inter == 0:
        return 0.0
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def estimate_distance_m(
    y_bottom: float,
    image_height: float,
    camera_height_m: float,
    vfov_deg: float,
    horizon_frac: float,
) -> float | None:
    """Flat-ground pinhole estimate of the distance to a box's bottom edge.

    `horizon_frac` is the horizon's row as a fraction of image height (0 = top).
    Returns None for points at or above the horizon, where the model breaks down.
    """
    horizon_y = horizon_frac * image_height
    if y_bottom <= horizon_y + 1:
        return None
    focal_px = (image_height / 2) / math.tan(math.radians(vfov_deg) / 2)
    return round(camera_height_m * focal_px / (y_bottom - horizon_y), 1)


@dataclass
class Track:
    track_id: int
    damage_type: str
    box: list[float]
    last_t: float
    observations: int = 1
    best_conf: float = 0.0


class HazardTracker:
    """Greedy IoU tracker that turns per-frame detections into unique hazards.

    A detection continues an existing track of the same class when it overlaps
    that track's last box by at least `iou_threshold` and the track was seen
    within `max_gap_s`; otherwise it starts a new hazard.
    """

    def __init__(self, iou_threshold: float = 0.3, max_gap_s: float = 1.0):
        self.iou_threshold = iou_threshold
        self.max_gap_s = max_gap_s
        self.tracks: dict[int, Track] = {}
        self._next_id = 1

    def update(self, t: float, detections: list[dict]) -> list[tuple[dict, Track, bool]]:
        """Assign each detection ({box, damage_type, confidence}) to a track.

        Returns (detection, track, is_new) for every detection.
        """
        live = [tr for tr in self.tracks.values() if t - tr.last_t <= self.max_gap_s]
        candidates = []
        for di, det in enumerate(detections):
            for tr in live:
                if tr.damage_type == det["damage_type"]:
                    overlap = iou(det["box"], tr.box)
                    if overlap >= self.iou_threshold:
                        candidates.append((overlap, di, tr.track_id))
        candidates.sort(reverse=True)

        assigned: dict[int, int] = {}
        used_tracks: set[int] = set()
        for _, di, tid in candidates:
            if di not in assigned and tid not in used_tracks:
                assigned[di] = tid
                used_tracks.add(tid)

        results = []
        for di, det in enumerate(detections):
            if di in assigned:
                tr = self.tracks[assigned[di]]
                tr.box, tr.last_t = det["box"], t
                tr.observations += 1
                tr.best_conf = max(tr.best_conf, det["confidence"])
                results.append((det, tr, False))
            else:
                tr = Track(self._next_id, det["damage_type"], det["box"], t, best_conf=det["confidence"])
                self.tracks[tr.track_id] = tr
                self._next_id += 1
                results.append((det, tr, True))
        return results
