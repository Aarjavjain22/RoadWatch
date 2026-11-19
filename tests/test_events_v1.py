import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from schemas.events import TOPIC_SCHEMAS


TS = "2026-10-15T14:03:22.410Z"
HEADER = {"schema_version": "1.0", "ts": TS}
FRAME = {
    **HEADER, "type": "FrameInput", "drive_id": "drive", "frame_id": "frame",
    "width": 640, "height": 480, "frame_index": 0, "pts_s": 0,
    "image_ref": "frames/frame.jpg", "replay_session_id": "run", "emitted_at": TS,
    "telemetry": {**HEADER, "type": "VehicleState"},
}
BATCH = {
    **HEADER, "type": "DetectionBatch", "batch_id": "batch", "frame": FRAME,
    "model_version": "test", "outcome": "success",
    "frame_result": {
        **HEADER, "type": "FrameResult", "frame_id": "frame", "vehicle_id": None,
        "lat": None, "lon": None, "status": "clean", "n_detections": 0,
        "model_version": "test", "latency_ms": 10, "image_ref": "frames/frame.jpg",
    },
}


def test_clean_and_failed_inference_are_distinct():
    adapter = TOPIC_SCHEMAS["roadwatch.detections"]
    assert adapter.validate_python(BATCH).frame_result.status == "clean"
    failed = dict(BATCH, outcome="failure", frame_result=None, failure_code="decode")
    assert adapter.validate_python(failed).frame_result is None
    with pytest.raises(ValidationError):
        adapter.validate_python(dict(failed, frame_result=BATCH["frame_result"]))


def test_reject_invalid_frame_or_box():
    with pytest.raises(ValidationError):
        TOPIC_SCHEMAS["roadwatch.frames"].validate_python(dict(FRAME, schema_version="2.0"))
    with pytest.raises(ValidationError, match="outside frame bounds"):
        TOPIC_SCHEMAS["roadwatch.detections"].validate_python(dict(
            BATCH,
            frame_result=dict(BATCH["frame_result"], status="damaged", n_detections=1),
            detections=[dict(obs_id="obs", hazard_type="pothole", confidence=0.8, x1=42, y1=42, x2=650, y2=90)],
        ))


def test_draft_requires_approval():
    with pytest.raises(ValidationError, match="require human approval"):
        TOPIC_SCHEMAS["roadwatch.triage"].validate_python({
            **HEADER, "type": "TriageDecision", "hazard_id": "H-012", "actions": ["draft_311"],
            "priority": "high", "reason": "Repeat sightings", "decided_by": "rules",
            "rules_changed": [], "needs_human_approval": False,
        })


@pytest.mark.parametrize("topic", TOPIC_SCHEMAS)
def test_published_json_schemas_match_models(topic):
    path = Path(__file__).resolve().parents[1] / "schemas" / "json" / f"{topic}.json"
    schema = json.loads(path.read_text())
    assert schema.pop("$schema") == "https://json-schema.org/draft/2020-12/schema"
    assert schema == TOPIC_SCHEMAS[topic].json_schema()
