import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.geometry import HazardTracker, estimate_distance_m, iou  # noqa: E402


def det(box, damage_type="Pothole", confidence=0.5):
    return {"box": box, "damage_type": damage_type, "confidence": confidence}


def test_iou_identical_disjoint_and_partial():
    assert iou([0, 0, 10, 10], [0, 0, 10, 10]) == 1.0
    assert iou([0, 0, 10, 10], [20, 20, 30, 30]) == 0.0
    assert abs(iou([0, 0, 10, 10], [5, 0, 15, 10]) - 50 / 150) < 1e-9


def test_tracker_continues_overlapping_same_class():
    tr = HazardTracker()
    (_, first, new1), = tr.update(0.0, [det([100, 100, 200, 200], confidence=0.4)])
    (_, second, new2), = tr.update(0.2, [det([105, 102, 205, 204], confidence=0.7)])
    assert new1 and not new2
    assert first.track_id == second.track_id
    assert second.observations == 2
    assert second.best_conf == 0.7


def test_tracker_splits_on_class_gap_or_distance():
    tr = HazardTracker(max_gap_s=1.0)
    tr.update(0.0, [det([100, 100, 200, 200])])
    (_, other_class, is_new), = tr.update(0.2, [det([100, 100, 200, 200], damage_type="Alligator")])
    assert is_new
    (_, far_away, is_new), = tr.update(0.4, [det([600, 600, 700, 700])])
    assert is_new
    (_, after_gap, is_new), = tr.update(5.0, [det([100, 100, 200, 200])])
    assert is_new
    assert len({other_class.track_id, far_away.track_id, after_gap.track_id}) == 3


def test_tracker_assigns_each_track_once_per_frame():
    tr = HazardTracker()
    tr.update(0.0, [det([100, 100, 200, 200])])
    results = tr.update(0.2, [det([100, 100, 200, 200]), det([110, 110, 210, 210])])
    assert [is_new for _, _, is_new in results].count(True) == 1


def test_distance_decreases_toward_bottom_and_rejects_horizon():
    kwargs = dict(image_height=720, camera_height_m=1.4, vfov_deg=50, horizon_frac=0.45)
    near = estimate_distance_m(700, **kwargs)
    far = estimate_distance_m(400, **kwargs)
    assert near is not None and far is not None and near < far
    assert estimate_distance_m(300, **kwargs) is None
