import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "streaming"))

from contracts import (  # noqa: E402
    EVENT_VERSION,
    classify_noaa_conditions,
    classify_visibility,
    classify_weather_code,
    ground_truth_agreement,
    severity_from_area,
    validation_error,
)


def base_event():
    return {
        "event_version": EVENT_VERSION,
        "event_id": "evt_frame_1_000",
        "trace_id": "frame_1",
        "frame_id": "frame_1",
        "damage_type": "Pothole",
        "confidence": 0.7,
        "road_segment": "segment_01",
    }


def test_severity_levels():
    assert severity_from_area(0.05, 0.9) == "High"
    assert severity_from_area(0.02, 0.5) == "Medium"
    assert severity_from_area(0.001, 0.3) == "Low"


def test_valid_event_passes():
    assert validation_error(base_event()) is None


def test_missing_field_rejected():
    event = base_event()
    event.pop("road_segment")
    assert validation_error(event) == "schema_invalid"


def test_confidence_out_of_range_rejected():
    event = base_event()
    event["confidence"] = 1.7
    assert validation_error(event) == "confidence_out_of_range"


def test_unsupported_version_rejected():
    event = base_event()
    event["event_version"] = "2.0"
    assert validation_error(event) == "unsupported_event_version"


def test_unknown_damage_type_rejected():
    event = base_event()
    event["damage_type"] = "Unknown"
    assert validation_error(event) == "unknown_damage_type"


def test_classify_weather_code_known_and_unknown():
    assert classify_weather_code(0) == "clear"
    assert classify_weather_code(73) == "snow"
    assert classify_weather_code(None) == "unknown"
    assert classify_weather_code(999) == "unknown"


def test_classify_visibility_clear_frame():
    condition, adjustment, escalate = classify_visibility(
        brightness=140, contrast=60, blur_score=200, glare_ratio=0.01, weather_desc="clear"
    )
    assert condition == "clear"
    assert adjustment == 1.0
    assert escalate is False


def test_classify_visibility_degraded_frame_escalates():
    condition, adjustment, escalate = classify_visibility(
        brightness=30, contrast=10, blur_score=200, glare_ratio=0.01, weather_desc="snow"
    )
    assert "low_light" in condition
    assert "low_contrast" in condition
    assert "snow" in condition
    assert 0.0 < adjustment < 1.0
    assert escalate is True


def test_classify_visibility_adjustment_floor():
    condition, adjustment, escalate = classify_visibility(
        brightness=10, contrast=5, blur_score=5, glare_ratio=0.5, weather_desc="heavy_snow"
    )
    assert adjustment >= 0.3
    assert escalate is True


def test_classify_visibility_confident_ml_verdict_counts():
    clean = dict(brightness=140, contrast=60, blur_score=200, glare_ratio=0.01, weather_desc="clear")
    condition, adjustment, _ = classify_visibility(**clean, ml_weather="fog", ml_confidence=0.9)
    assert condition == "ml_fog"
    assert adjustment == 0.8


def test_classify_visibility_ignores_weak_or_benign_ml_verdict():
    clean = dict(brightness=140, contrast=60, blur_score=200, glare_ratio=0.01, weather_desc="clear")
    assert classify_visibility(**clean, ml_weather="snow", ml_confidence=0.4)[0] == "clear"
    assert classify_visibility(**clean, ml_weather="cloudy", ml_confidence=0.99)[0] == "clear"


def test_classify_visibility_ignores_ml_verdict_in_low_light():
    night = dict(brightness=20, contrast=60, blur_score=200, glare_ratio=0.01, weather_desc="clear")
    condition, adjustment, escalate = classify_visibility(**night, ml_weather="rain", ml_confidence=0.99)
    assert condition == "low_light"
    assert adjustment == 0.8
    assert escalate is False


def test_classify_noaa_conditions():
    assert classify_noaa_conditions(0, 0) == "clear"
    assert classify_noaa_conditions(5.0, 0) == "rain"
    assert classify_noaa_conditions(0, 12.0) == "snow"
    assert classify_noaa_conditions(2.0, 8.0) == "snow+rain"
    assert classify_noaa_conditions(None, None) == "clear"


def test_ground_truth_agreement_matches():
    agrees, _ = ground_truth_agreement("clear", agent_adverse_pct=0.05)
    assert agrees is True

    agrees, _ = ground_truth_agreement("snow", agent_adverse_pct=0.8)
    assert agrees is True


def test_ground_truth_agreement_mismatches():
    agrees, note = ground_truth_agreement("snow", agent_adverse_pct=0.02)
    assert agrees is False
    assert "NOAA reports" in note

    agrees, note = ground_truth_agreement("clear", agent_adverse_pct=0.9)
    assert agrees is False
    assert "NOAA reports clear" in note
