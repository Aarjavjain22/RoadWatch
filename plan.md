# RoadWatch Buffalo semester plan

**Status:** Integration planning. See [handoff-integration.md](handoff-integration.md) for the handoff review, reuse decisions, branch workflow, and prerequisite for importing the teammate’s code. Owners and thresholds remain proposed.

## Semester outcome

Replay a privacy-reviewed Buffalo drive through Kafka. Detect seven hazard classes, assign repeated sightings to persistent street-snapped pins, and show map alerts and human-reviewable 311 drafts in Grafana. Deliver a held-out model comparison and measure time from replayed camera frame to a committed pin.

Scope covers local archive ingestion, public-data seeding, spatial mapping, dashboard alerting, and model training and inference. V2V, CARLA, and the weather trust agent are outside this semester plan. KBUF weather is used for frame metadata, sampling, and evaluation. Dashboard alerts and draft review are in scope. Live traffic-camera integration, in-car deployment, automated municipal submissions, and AI triage are deferred. Preserve the supplied weather/classification subsystem as optional teammate functionality after baseline review. New trust-agent development remains outside the Aarjav/Rishi scope. The dataset starting point is the existing download on Aarjav’s PC.

## Owners and handoffs

| Owner | Main work | Handoff |
| --- | --- | --- |
| Aarjav | Repo and Compose structure, PostGIS initialization and seed, archive inspection, GPS sync, privacy, frame sampling, Kafka replay, mapper, lifecycle, rules-based triage, 311 drafts, Grafana and integration | Give Rishi a versioned, approved frame manifest with image refs, timestamps, weather, dimensions, drive IDs, and frozen split assignments. Publish frame and detection fixtures early. |
| Rishi | Labeling rubric and quality review, YOLO-World candidate labels, four-class baseline, seven-class YOLOv8s fine-tune, held-out evaluation, ONNX parity and benchmark, Kafka inference worker | Give Aarjav schema-valid inference fixtures with positive, clean, and failed frames, then a versioned model bundle, worker configuration and benchmark report. |
| Both | Event contract review, class and rare-data policy, labeling and adjudication, camera/GPS calibration fixture, staged end-to-end replay | Agree on contract and a small permitted pilot set before model development depends on ingestion. Review exceptions and sign off on the final evaluation. |

## Schedule and gates

| Weeks | Aarjav | Rishi | Exit gate |
| --- | --- | --- | --- |
| 1–2 | Inventory a small local subset, assess GPS and storage, define Compose/database approach and event schemas. | Locate RDD2022 weights and GPU, define label rubric and class order, agree on event fixtures. | Verified sample with timing and GPS or a documented fallback. Both agree on schemas, split policy and pilot inputs. |
| 3–4 | Apply DDL, import seed, implement privacy/GPS/frame pipeline, export first pilot labeling batch. | Review calibration labels, prepare drive-group split and evaluation tooling, define YOLO-World prompt mapping. | Seed validates. A small privacy-reviewed, geolocated batch round-trips through chosen labeling tool. |
| 5–6 | Ingest eligible drives, export ~1,000 diverse frames, build scheduled replay and start mapper against fixtures. | Bootstrap rare candidates, review labels including negatives, run four-class baseline, assemble frozen datasets. | Repeatable frame manifest and labeling export. Replay works at 1× and controlled accelerated rate. |
| 7–8 | Implement merges, lifecycle, rule triage and human approval queue. | Fine-tune YOLOv8s, evaluate by class and weather, export ONNX and integrate worker. | One replay produces valid positive and clean results, persistent pins, and a dashboard-facing alert. |
| 9–10 | Implement dashboard panels and run GPS, Kafka retry and lifecycle stress tests. | Verify PyTorch/ONNX parity, benchmark CPU and available GPU/edge provider, select a release artifact on validation data. | End-to-end scenarios pass and latency evidence includes a 1× run. Report measured bottlenecks and any edge hardware limitations. |
| 11–12 | Final demo, documented setup and replay procedure, mapper and latency report. | Frozen test evaluation, model card, reproducibility notes and demo artifact. | Both can reproduce the demo from named inputs. Report missing strata, failed targets and any limits in field validity. |

## Release evidence

- PostGIS seed works on a fresh database, reruns safely and passes documented count, geometry and foreign-key checks.
- A pilot drive demonstrates dated GPS alignment, privacy review, road snapping, frame extraction, weather provenance, and a labeling round-trip. Final sampling reports the actual count and strata coverage against the ~1,000-frame goal.
- All five Event Format v1 topic contracts pass fixtures and invalid-payload validation. Interruption and redelivery leave one logical frame result and one copy of each observation.
- A labeled mapping fixture demonstrates pin creation, same-hazard merge, nearby separate hazards, confirmation, resolution via qualified clean passes, false-positive drop, alert deduplication, and draft approval without external transmission.
- Report map error against independent field anchors where available, plus duplicate and false-merge rates on annotated replay. Use 311 as contextual evidence.
- Proposed live acceptance statistic is p95 camera-to-pin under 5 s at 1× with the hardware, sample size, unmatched-frame rate and lag disclosed. Report candidate and confirmation delays separately. The deck also targets camera-to-alert under 5 s, so measure that separately and identify delays due to confirmation policy.
- Compare original four-class performance with the new model on the same held-out Buffalo subset. Report seven-class support and metrics by class and weather, ONNX parity, FPS, p95 inference latency and operating conditions. The deck’s 10+ FPS target applies to identified in-car-class hardware when available.

## Decisions and contingencies

| When | Decision or risk | Planned response |
| --- | --- | --- |
| Pilot | Where are the media, GPS, and capture timestamps? | Inventory PC copies first. If footage lacks usable geolocation, retain it for permitted labeling and use a known synchronized drive or fixture for mapper development. Report the geolocated share. |
| Pilot | Who may use or share the clips? | Aarjav confirms rights and storage policy before distribution. Give Rishi only approved derivatives. |
| Contract review | Can the old weights, a GPU, and reliable camera mounting/calibration be obtained? | Record artifact provenance and compute access. If weights are unavailable, disclose the substitute baseline. If calibration is unavailable, use a fixture to test geometry and keep real low-quality locations out of automated decisions. |
| Label audit | Are snow, rain, night or rare classes absent? | Report gaps, collect eligible data or use licensed training-only supplements. Preserve Buffalo-only held-out metrics and show support per stratum. |
| Integration | What thresholds make sense for confirmation, clean passes, drift and confidence? | Tune against labeled development routes and freeze policies before final evaluation. With one vehicle, report independent drives separately from vehicle count. |
| Release | Are legacy summer code and infrastructure available? | The slide deck describes them, but they are absent from this checkout. Obtain the complete teammate base and reconcile it with the archive overlay before implementation. Reuse verified legacy components. |

See [architecture.md](architecture.md), [event-contract.md](event-contract.md), and [data-and-model-plan.md](data-and-model-plan.md).
