"""RoadWatch Buffalo v1 payloads, independent of the legacy Kafka contracts."""

from datetime import timezone
from typing import Annotated, Literal

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, model_validator


HazardType = Literal["longitudinal", "transverse", "alligator", "pothole", "debris", "animal", "crash"]
Priority = Literal["low", "medium", "high"]
State = Literal["candidate", "confirmed", "reported", "resolved", "dropped"]
Nonnegative = Annotated[float, Field(ge=0)]
Probability = Annotated[float, Field(ge=0, le=1)]
Count = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]
Latitude = Annotated[float, Field(ge=-90, le=90)]
Longitude = Annotated[float, Field(ge=-180, le=180)]
UtcDatetime = Annotated[AwareDatetime, AfterValidator(lambda value: value.astimezone(timezone.utc))]


class Payload(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)


class Event(Payload):
    schema_version: Literal["1.0"]
    ts: UtcDatetime


class VehicleState(Event):
    type: Literal["VehicleState"]
    vehicle_id: str | None = None
    lat: Latitude | None = None
    lon: Longitude | None = None
    heading_deg: Annotated[float, Field(ge=0, lt=360)] | None = None
    speed_kph: Nonnegative | None = None
    gps_accuracy_m: Nonnegative | None = None
    osm_way_id: int | None = None

    @model_validator(mode="after")
    def paired_coordinates(self):
        if (self.lat is None) != (self.lon is None):
            raise ValueError("lat and lon must both be set or both be null")
        return self


class FrameInput(Event):
    type: Literal["FrameInput"]
    frame_id: str
    drive_id: str
    image_ref: str
    width: PositiveInt
    height: PositiveInt
    frame_index: Count
    pts_s: Nonnegative
    replay_session_id: str
    emitted_at: UtcDatetime
    telemetry: VehicleState
    gps_quality: Literal["matched", "unmatched", "unknown"] = "unknown"
    weather_hour: UtcDatetime | None = None


class FrameResult(Event):
    type: Literal["FrameResult"]
    frame_id: str
    vehicle_id: str | None
    lat: Latitude | None
    lon: Longitude | None
    status: Literal["damaged", "clean"]
    n_detections: Count
    model_version: str
    latency_ms: Nonnegative
    image_ref: str


class BoxDetection(Payload):
    obs_id: str
    hazard_type: HazardType
    confidence: Probability
    x1: Nonnegative
    y1: Nonnegative
    x2: Nonnegative
    y2: Nonnegative


class DetectionBatch(Event):
    type: Literal["DetectionBatch"]
    batch_id: str
    frame: FrameInput
    model_version: str
    outcome: Literal["success", "failure"]
    frame_result: FrameResult | None = None
    failure_code: str | None = None
    detections: list[BoxDetection] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_outcome(self):
        if self.outcome == "failure":
            if not self.failure_code or self.frame_result is not None or self.detections:
                raise ValueError("a failed frame needs a failure code and no detections or frame result")
            return self

        result = self.frame_result
        if self.failure_code or result is None:
            raise ValueError("successful inference requires a frame result")
        if (
            result.frame_id != self.frame.frame_id
            or result.model_version != self.model_version
            or result.ts != self.frame.ts
            or result.n_detections != len(self.detections)
            or result.status != ("damaged" if self.detections else "clean")
        ):
            raise ValueError("frame result does not match the inference batch")
        if len({item.obs_id for item in self.detections}) != len(self.detections):
            raise ValueError("duplicate observation IDs")
        for item in self.detections:
            if not (item.x1 < item.x2 <= self.frame.width and item.y1 < item.y2 <= self.frame.height):
                raise ValueError("detection box is outside frame bounds or has zero area")
        return self


class HazardObservation(Event):
    type: Literal["HazardObservation"]
    obs_id: str
    frame_id: str
    vehicle_id: str | None
    hazard_type: HazardType
    confidence: Probability
    est_lat: Latitude | None
    est_lon: Longitude | None
    location_uncertainty_m: Nonnegative | None
    distance_m: Nonnegative | None
    in_ego_path: bool | None
    weather: dict | None = None
    model_version: str


class HazardEvent(Event):
    type: Literal["HazardEvent"]
    hazard_id: str
    event: Literal["created", "updated", "confirmed", "reported", "resolved", "dropped"]
    hazard_type: HazardType
    lat: Latitude
    lon: Longitude
    uncertainty_m: Nonnegative
    n_observations: Count
    n_vehicles: Count
    max_confidence: Probability
    severity: Priority
    state: State
    street: str | None
    first_seen: UtcDatetime
    last_seen: UtcDatetime
    linked_311_case: str | None
    transition_id: str | None = None
    revision: PositiveInt | None = None
    osm_way_id: int | None = None


class TriageDecision(Event):
    type: Literal["TriageDecision"]
    hazard_id: str
    actions: list[Literal["ignore", "watch", "alert_drivers", "draft_311"]]
    priority: Priority
    reason: str
    decided_by: Literal["rules", "agent", "agent + rules"]
    rules_changed: list[str]
    needs_human_approval: bool
    decision_id: str | None = None
    draft_id: str | None = None

    @model_validator(mode="after")
    def drafts_require_approval(self):
        if "draft_311" in self.actions and not self.needs_human_approval:
            raise ValueError("311 drafts require human approval")
        return self


class WarningEvent(Event):
    type: Literal["WarningEvent"]
    warning_id: str
    hazard_id: str
    target_vehicle_id: str
    distance_m: Nonnegative
    action: Literal["MAINTAIN", "CAUTION", "REDUCE_SPEED", "PREPARE_TO_STOP", "STOP"]
    urgency: Literal["normal", "high"]
    reason: list[str]
    source: Literal["rules", "agent"]
    validated_by_rules: Literal[True]


class DashboardAlert(Event):
    type: Literal["DashboardAlert"]
    alert_id: str
    hazard_id: str
    revision: PositiveInt
    priority: Priority
    reason: str


TOPIC_SCHEMAS = {
    "roadwatch.frames": TypeAdapter(FrameInput),
    "roadwatch.detections": TypeAdapter(DetectionBatch),
    "roadwatch.hazards": TypeAdapter(HazardEvent),
    "roadwatch.triage": TypeAdapter(TriageDecision),
    "roadwatch.alerts": TypeAdapter(Annotated[WarningEvent | DashboardAlert, Field(discriminator="type")]),
}
