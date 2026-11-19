# Event Format v1 contract plan

Aarjav and Rishi should agree on this mapping before writing Pydantic models. The prototype’s Events tab uses six message types and short topic names. This pipeline uses the five requested Kafka topics. Existing shapes retain their field names and semantics. The raw frame and inference batch are new messages because the prototype starts after inference.

| Kafka topic | Producer → consumers | v1 shape and prototype relationship |
| --- | --- | --- |
| `roadwatch.frames` | Replay → detector | New `FrameInput`. Carries the prototype’s `VehicleState` telemetry inline plus frame media metadata. |
| `roadwatch.detections` | Detector → mapper, dashboard | New `DetectionBatch`, one terminal outcome per frame. Successful batches carry a prototype-compatible `FrameResult` and pixel-box evidence. The mapper constructs the geospatial `HazardObservation` representation after projection and persists it. |
| `roadwatch.hazards` | Mapper → dashboard, triage | Prototype `HazardEvent` with additive transition ID, revision and OSM way ID. |
| `roadwatch.triage` | Triage → dashboard, approval queue | Prototype `TriageDecision` with additive decision and draft IDs. |
| `roadwatch.alerts` | Triage → dashboard | Prototype `WarningEvent` for an eligible replay target. A separate `DashboardAlert` represents targetless map alerts without fabricating a vehicle or distance. |

Before implementing the legacy adapter, inspect the teammate’s `streaming/contracts.py` and its existing `raw_frames`, `road_damage_events` and `frame_weather_context` messages. Preserve legacy topic behavior and event IDs. Enable v1 mapping through a separate consumer or producer path with explicit topic configuration. See [handoff-integration.md](handoff-integration.md).

## Payload design

- **Common.** Require literal `schema_version: "1.0"`, a discriminating `type`, an aware UTC ISO-8601 `ts`, stable IDs, finite numeric values and defined null semantics. WGS84 positions are paired `lat`/`lon` fields and uncertainty is in meters. Normalize heading to [0, 360) clockwise from true north and use km/h for speed. Accept future additive optional fields through reader compatibility. Changes to names or meanings require a new version.
- **FrameInput.** Require `frame_id`, `drive_id`, `image_ref`, width, height, capture `ts`, frame index/PTS, replay session and emission timestamp. Carry `vehicle_id`, `lat`, `lon`, `heading_deg`, `speed_kph`, `gps_accuracy_m`, `osm_way_id`, GPS quality and optional `weather_hour`. Explicitly represent unknown telemetry. Raw local camera and GPS paths stay out of broker payloads.
- **DetectionBatch.** Reference frame and replay IDs, echo capture and emission times and the GPS envelope, and record inference outcome (success or failure), `model_version`, timing and stable batch ID. On success include `FrameResult` with `frame_id`, `vehicle_id`, `ts`, `lat`, `lon`, `status` (damaged or clean), `n_detections`, `model_version`, `latency_ms` and `image_ref`. State which image ref is the annotated derivative when produced. Items contain stable `obs_id`, `hazard_type`, `confidence` and ordered `x1,y1,x2,y2` in the original decoded image space. A healthy empty batch is clean. A failed batch has a failure code and no `FrameResult`. Validate counts and box bounds.
- **HazardObservation.** Preserve the prototype’s `obs_id`, `frame_id`, `vehicle_id`, `ts`, `hazard_type`, `confidence`, `est_lat`, `est_lon`, `location_uncertainty_m`, `distance_m`, `in_ego_path`, optional `weather` and `model_version`. Unavailable estimates and lane evidence remain explicitly unknown and cannot create a pin. Keep `weather.trust_adjust` compatible when reading prototype examples and leave it unset in this workflow.
- **HazardEvent.** Preserve `hazard_id`, `event` (created, updated, confirmed, reported, resolved, dropped), `ts`, `hazard_type`, `lat`, `lon`, `uncertainty_m`, `n_observations`, `n_vehicles`, `max_confidence`, `severity`, `state`, `street`, `first_seen`, `last_seen` and nullable `linked_311_case`.
- **TriageDecision.** Preserve `hazard_id`, `ts`, `actions` (ignore, watch, alert_drivers, draft_311), `priority`, `reason`, `decided_by`, `rules_changed` and `needs_human_approval`. Producers use `decided_by: rules`. A draft action requires approval.
- **WarningEvent.** Preserve `warning_id`, `hazard_id`, `target_vehicle_id`, `ts`, `distance_m`, `action`, `urgency`, `reason`, `source` and `validated_by_rules`. Support the prototype action enum when reading (MAINTAIN, CAUTION, REDUCE_SPEED, PREPARE_TO_STOP, STOP). Publishing is for replay/dashboard evaluation only. `DashboardAlert` should carry an alert ID, hazard ID and revision, timestamp, priority and reason. Use rules and deduplicate alerts per episode/revision policy.

Canonical class order for schemas and YOLO labels, IDs 0–6, is longitudinal, transverse, alligator, pothole, debris, animal, crash. Record and version any alias normalization before training or inference.

## Delivery and verification

Key frame and detection topics by replay session and drive to preserve order. Key the other three by hazard ID. Separate replay delivery IDs from physical evidence IDs, so broker retries and repeated historical footage cannot inflate vehicle or pass counts. Record one model selection per replay run. Use explicit transition revisions for monotonic dashboard updates.

Implementation will generate JSON Schema from Pydantic v2 and check generated schemas against producer and consumer fixtures. Copy the prototype examples into fixtures for compatible shapes, including its `VehicleState` example for telemetry and its `FrameResult` and `HazardObservation` examples for per-frame and geospatial evidence. Add end-to-end fixtures for a replayed frame with and without usable GPS, clean and failed inference, duplicate and late results, resolved pins and draft approval. Test invalid schema versions, coordinates, boxes, class IDs and missing required fields. Decide safe consumer behavior for unknown types before enabling the worker.
