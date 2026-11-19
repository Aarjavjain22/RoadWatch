# Buffalo handoff review and integration

## Evidence available

All six ZIPs passed integrity checks. Their 1,011 payload paths and sizes match the embedded manifest, plus the manifest itself. The payload includes 947 JPEGs grouped by eight clip names. The later 18-chunk upload reconstructed into an archive containing six inner bundles. Their manifests verify the supplied platform, weights, frames, evaluation results, UI source and documentation. The platform and UI source now occupy their runtime paths. Cached bytecode and large artifacts are omitted from Git. The media, checkpoints and evaluation outputs remain outside Git. The CSV headers record night/rain tags and notes. They contain no damage bounding boxes. Frame privacy and model behavior remain unverified.

The handoff notes name Ziyad’s workstation and branch, while Aarjav identifies Aditya’s weather work. Confirm the source ref and commit with the teammate to establish provenance. The initial overlay was incomplete. The later platform bundle supplies the upstream Compose stack, SQL, contracts, consumers and tests. The public-data seed now runs independently of the overlay. The supplied schema and builder are in `db/`. A clean PostGIS import matched all seven snapshot table counts plus provenance and audit rows, and a repeat import inserted zero rows.

## Reuse and scope

| Handoff component | Integration decision |
| --- | --- |
| Exported frames and frame-label CSV | Reuse frames for a small pilot and labeling after checking permissions, blurring, source-clip grouping, decode and frame offsets. Check CSV columns before using it. The brief says the footage lacks damage labels. The 947 images are a starting pool for the ~1,000 reviewed-frame goal. |
| Baseline `model/weights/best.pt` and detector evaluation | Verify provenance, class mapping and evaluation methodology. Reuse the baseline and helpers when compatible. Detection-by-condition artifacts establish a reference for behavioral evaluation. Held-out accuracy still requires reviewed human boxes and negatives. |
| Buffalo weather weights, classifier split, validation dashboard and results | Retain the teammate’s work as an optional legacy subsystem once its base is reproducible. Validate supplied weights and results with their documented exclude list. Rishi still needs an independent drive-level split for seven-class damage detection. Keep historical KBUF metadata distinct from estimated visual weather and current Open-Meteo observations. |
| GoPro video mode and Kafka plumbing | Reuse existing decoder, sampling and reliable producer code after obtaining the referenced source. Extend the ingestion path for local GPS, privacy and capture-time provenance. The legacy pipeline now runs alongside provisioned v1 topics. Implement the GPS-aware adapter when ingest and replay are ready, preserving legacy IDs and consumers. |
| FastAPI UI and Grafana | Review the UI’s existing queries and dashboards before changing service wiring. Reuse approved previews and read-only views when possible. Implement the planned hazard map, lifecycle and draft queue against PostGIS. |
| Seven-class detector and mapper | Train after annotation and split verification. ONNX inference, geospatial pinning, qualified clean passes and approval-only drafts remain part of Aarjav and Rishi’s extension. V2V and CARLA remain outside scope. |

## Implementation gates

1. **Receive and verify.** Complete. All 18 chunks reconstructed successfully. Archive entries and inner manifests were checked before importing the platform and UI source.
2. **Integrate the base.** Complete. The legacy Compose stack now uses PostGIS and loopback port bindings. Its tables and the Buffalo public-data seed coexist. The five Event Format v1 topics are provisioned alongside the existing Kafka topics and have checked-in Pydantic models and JSON schemas.
3. **Validate the teammate baseline.** The 41 supplied tests and eight v1 tests pass. A two-frame replay produced two detections and two weather contexts. PostGIS joined both detections to their weather context and the local UI reported both rows. Kafka, Grafana and Prometheus readiness checks passed. The external Open-Meteo request received a proxy 403. The classifier and local image heuristics continued processing. The offline weather classifier run also reproduced all bundled metrics on 586 frames after the supplied split exclusion (night F1 1.000, rain F1 0.900, degraded-condition F1 0.994). This evaluation uses clip-level condition labels.
4. **Integrate Aarjav and Rishi’s vertical slice.** Verify a labeled fixture traverses legacy input or the v1 replay path, detector output, PostGIS merge, lifecycle and Grafana. Define the adapter against actual code first. Lack of reliable GPS in a legacy frame limits it to preview and evaluation. Establish capture-clock and GPS availability on a small PC sample before mapping original footage.
5. **Finish the semester scope.** Follow the existing labeling, model, and integration milestones with the verified baseline and data. Compare measured mapping and model outcomes with the acceptance criteria in [plan.md](plan.md).

## Branch workflow

`V2` is the integration branch. `feature/buffalo-handoff` starts from it and is the working branch for this integration. Validate changes there, merge or fast-forward them into `V2`, and publish only `V2` during development. Promote the validated integration branch to `main` at the end when the full acceptance criteria pass.
