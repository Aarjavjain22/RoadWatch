import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "streaming"))
sys.path.insert(0, str(ROOT / "model"))

import numpy as np  # noqa: E402

import pytest  # noqa: E402

from contracts import video_frame_id, video_road_segment, video_slug  # noqa: E402
from eval_detection_buffalo import condition_group, strip_view, summarize  # noqa: E402
from eval_weather_buffalo import (  # noqa: E402
    binary_metrics, clip_labels, default_predictions_path, load_sheet, predicted_labels, used_in_training,
)
from image_heuristics import crop_letterbox  # noqa: E402
from prepare_weather_cls_buffalo import clip_class, plan_split  # noqa: E402

CLIPS = {
    "Video 1 Morning.mp4": 366.0, "Video 2 Cropped Morning Rainy.mp4": 312.0, "Video 3 Morning.mp4": 126.4,
    "Video 4 Morning.mp4": 1044.0, "Video 6 Night.mp4": 984.0,
}


def test_crop_letterbox_removes_black_bar_and_its_blended_edge():
    frame = np.zeros((100, 160, 3), np.uint8)
    frame[40:] = 150
    # The bar is 40 rows; two more are dropped because compression smears the bar into them.
    assert crop_letterbox(frame).shape[:2] == (58, 160)


def test_crop_letterbox_removes_bar_above_dark_content():
    # Under a bridge the scene next to the bar is nearly as dark as the bar.
    frame = np.zeros((100, 160, 3), np.uint8)
    frame[40:60], frame[60:] = 12, 150
    assert crop_letterbox(frame).shape[:2] == (58, 160)


def test_crop_letterbox_keeps_dark_but_noisy_night_sky():
    frame = np.full((100, 160, 3), 30, np.uint8)
    sky = np.random.default_rng(0).integers(0, 6, (30, 160), dtype=np.uint8)
    frame[:30] = sky[:, :, None]
    assert crop_letterbox(frame).shape[:2] == (100, 160)


def test_crop_letterbox_leaves_normal_and_all_black_frames():
    assert crop_letterbox(np.full((90, 120, 3), 120, np.uint8)).shape[:2] == (90, 120)
    assert crop_letterbox(np.zeros((90, 120, 3), np.uint8)).shape[:2] == (90, 120)


def test_video_frame_id_is_stable_and_readable():
    assert video_slug("Video 2 Cropped Morning Rainy.mp4") == "video_2_cropped_morning_rainy"
    assert video_frame_id("Video 6 Night.mp4", 412000) == "video_6_night_00412000"
    assert video_frame_id("Video 6 Night.mp4", 412000) == video_frame_id("/app/videos/Video 6 Night.mp4", 412000)


def test_video_road_segment():
    assert video_road_segment("Video 6 Night.mp4") == "buffalo_v06"
    assert video_road_segment("drive.mp4") == "buffalo"


def test_clip_labels_from_file_names():
    assert clip_labels("Video 6 Night.mp4") == {"night": True, "rain": False, "degraded": True}
    assert clip_labels("Video 2 Cropped Morning Rainy.mp4") == {"night": False, "rain": True, "degraded": True}
    assert clip_labels("Video 1 Morning.mp4") == {"night": False, "rain": False, "degraded": False}


def test_predicted_labels_follow_visibility_reasons():
    assert predicted_labels("low_light+blurry") == {"night": True, "rain": False, "degraded": True}
    assert predicted_labels("ml_rain") == {"night": False, "rain": True, "degraded": True}
    assert predicted_labels("clear") == {"night": False, "rain": False, "degraded": False}


def test_binary_metrics():
    m = binary_metrics([(True, True), (True, False), (False, True), (False, False)])
    assert (m["tp"], m["fn"], m["fp"], m["tn"]) == (1, 1, 1, 1)
    assert m["precision"] == 0.5 and m["recall"] == 0.5 and m["accuracy"] == 0.5


def test_binary_metrics_without_positives_reports_none():
    m = binary_metrics([(False, False), (False, False)])
    assert m["precision"] is None and m["recall"] is None and m["f1"] is None
    assert m["accuracy"] == 1.0


def test_load_sheet(tmp_path):
    sheet = tmp_path / "labels.csv"
    sheet.write_text(
        "frame_id,video,offset_s,image_path,label_night,label_rain,notes\n"
        "video_6_night_00000000,Video 6 Night.mp4,0,x.jpg,1,0,\n"
        "video_1_morning_00005000,Video 1 Morning.mp4,5,y.jpg,0,yes,puddles\n",
        encoding="utf-8",
    )
    labels = load_sheet(sheet)
    assert labels["video_6_night_00000000"] == {"night": True, "rain": False, "degraded": True}
    assert labels["video_1_morning_00005000"] == {"night": False, "rain": True, "degraded": True}


def test_clip_class_from_file_names():
    assert clip_class("Video 2 Cropped Morning Rainy.mp4") == "rain"
    assert clip_class("Video 1 Morning.mp4") == "clear"
    assert clip_class("Video 1 Morning.mp4", dry_class="cloudy") == "cloudy"
    assert clip_class("Video 6 Night.mp4") is None


def test_plan_split_holds_out_clips_and_time_splits_the_only_rainy_clip():
    plan = plan_split(CLIPS, {"Video 1 Morning.mp4", "Video 3 Morning.mp4"}, single_clip_frac=0.6)
    assert plan == {
        "Video 2 Cropped Morning Rainy.mp4": {"class": "rain", "start_s": 0.0, "end_s": 187.0},
        "Video 4 Morning.mp4": {"class": "clear", "start_s": 0.0, "end_s": 1044.0},
    }


def test_plan_split_refuses_to_train_on_every_clip_of_a_class():
    with pytest.raises(ValueError, match="none is held out"):
        plan_split(CLIPS, set(), single_clip_frac=0.6)
    with pytest.raises(ValueError, match="not found"):
        plan_split(CLIPS, {"Video 9 Morning.mp4"}, single_clip_frac=0.6)


def test_used_in_training_covers_the_range_and_its_guard_gap():
    split = {"guard_sec": 30, "train": {
        "Video 2 Cropped Morning Rainy.mp4": {"class": "rain", "start_s": 0.0, "end_s": 187.0},
        "Video 4 Morning.mp4": {"class": "clear", "start_s": 0.0, "end_s": 1044.0},
    }}
    rainy = "Video 2 Cropped Morning Rainy.mp4"
    assert used_in_training(split, rainy, 0) and used_in_training(split, rainy, 186)
    assert used_in_training(split, rainy, 216.9)            # inside the guard gap
    assert not used_in_training(split, rainy, 217)
    assert used_in_training(split, "video_4_morning", 500)  # exported frames only carry the slug
    assert not used_in_training(split, "Video 1 Morning.mp4", 10)


def test_condition_group_from_file_names():
    assert condition_group("Video 6 Night.mp4") == "night"
    assert condition_group("Video 2 Cropped Morning Rainy.mp4") == "rain"
    assert condition_group("Video 1 Morning.mp4") == "dry_day"


def test_strip_view_keeps_only_the_band_at_the_bottom_of_a_black_frame():
    frame = np.arange(100, dtype=np.uint8).reshape(100, 1, 1).repeat(40, axis=1).repeat(3, axis=2) + 100
    view = strip_view(frame, (0.3, 0.7))
    assert view.shape == frame.shape
    assert not view[:60].any()
    assert (view[60:] == frame[30:70]).all()


def test_summarize_detections_per_frame():
    detections = [
        {"frame_id": "a", "damage_type": "Pothole", "confidence": 0.8, "severity": "High",
         "confidence_adjustment": 0.8, "adjusted_confidence": 0.64, "escalation_flag": False},
        {"frame_id": "a", "damage_type": "Transverse", "confidence": 0.3, "severity": "Low",
         "confidence_adjustment": 0.8, "adjusted_confidence": 0.24, "escalation_flag": False},
        {"frame_id": "b", "damage_type": "Pothole", "confidence": 0.4, "severity": "Low",
         "confidence_adjustment": 0.5, "adjusted_confidence": 0.2, "escalation_flag": True},
    ]
    s = summarize(10, detections)
    assert s["frames_with_detection_pct"] == 20.0
    assert s["detections_per_100_frames"] == 30.0
    assert s["confident_detections_per_100_frames"] == 10.0
    assert s["median_confidence"] == 0.4
    assert s["by_class"] == {"Pothole": 2, "Transverse": 1}
    assert s["mean_confidence_adjustment"] == 0.7
    assert s["on_escalated_frames_pct"] == 33.3


def test_summarize_without_detections_or_weather():
    s = summarize(5, [])
    assert s["detections"] == 0 and s["median_confidence"] is None
    assert "mean_confidence_adjustment" not in s


def test_default_predictions_path_follows_the_report_name():
    name = lambda p: default_predictions_path(Path(p)).name  # noqa: E731
    assert name("results/buffalo_weather_eval.json") == "buffalo_weather_predictions.csv"
    assert name("results/buffalo_weather_eval_unseen.json") == "buffalo_weather_predictions_unseen.csv"
    assert name("results/report.json") == "report_predictions.csv"
