# Buffalo handoff review and integration

## Evidence available

Reviewed the separately uploaded README, sharing note, integration checklist, agent brief, archive builder, and JSON manifest. The manifest lists 1,011 entries totaling 136,571,617 bytes before compression, including 947 exported JPEGs from eight clip-name groups. It records paths and byte sizes. The archive exceeds the 32 MiB attachment transfer limit, so archive contents, model weights, frame privacy, and runnable source remain unverified. The separately attached environment example was unavailable. The manifest lists a copy inside the archive.

The handoff notes name Ziyad’s workstation and branch, while Aarjav identifies Aditya’s weather work. Confirm the source ref and commit with the teammate to establish provenance. The provided notes describe an overlay and explicitly call for merging additional upstream files. The current checkout contains the original README and our planning documents.

## Reuse and scope

| Handoff component | Integration decision |
| --- | --- |
| Exported frames and frame-label CSV | Reuse frames for a small pilot and labeling after checking permissions, blurring, source-clip grouping, decode and frame offsets. Check CSV columns before using it. The brief says the footage lacks damage labels. The 947 images are a starting pool for the ~1,000 reviewed-frame goal. |
| Baseline `model/weights/best.pt` and detector evaluation | Verify provenance, class mapping and evaluation methodology. Reuse the baseline and helpers when compatible. Detection-by-condition artifacts establish a reference for behavioral evaluation. Held-out accuracy still requires reviewed human boxes and negatives. |
| Buffalo weather weights, classifier split, validation dashboard and results | Retain the teammate’s work as an optional legacy subsystem once its base is reproducible. Validate supplied weights and results with their documented exclude list. Rishi still needs an independent drive-level split for seven-class damage detection. Keep historical KBUF metadata distinct from estimated visual weather and current Open-Meteo observations. |
| GoPro video mode and Kafka plumbing | Reuse existing decoder, sampling and reliable producer code after obtaining the referenced source. Extend the ingestion path for local GPS, privacy and capture-time provenance. Adapt legacy messages at the boundary while keeping existing IDs and consumers working. |
| FastAPI UI and Grafana | Review the UI’s existing queries and dashboards before changing service wiring. Reuse approved previews and read-only views when possible. Implement the planned hazard map, lifecycle and draft queue against PostGIS. |
| Seven-class detector and mapper | Train after annotation and split verification. ONNX inference, geospatial pinning, qualified clean passes and approval-only drafts remain part of Aarjav and Rishi’s extension. V2V and CARLA remain outside scope. |

## Implementation gates

1. **Receive and verify.** Provide a workspace-accessible archive link or split uploads under 32 MiB each. Also provide the teammate’s Git remote and exact ref or a source-only snapshot of the complete base with dependency manifests. The manifest omits `docker-compose.yml`, `db/init.sql`, `streaming/producer.py`, `streaming/contracts.py`, and `streaming/analytics_consumer.py`. The brief also references `streaming/yolo_consumer.py`, `streaming/requirements.txt`, and `tests/test_pipeline.py`, absent from this manifest. A complete base closes these gaps.
2. **Stage and review.** List archive entries and validate paths and sizes before extraction outside the working tree. Compare the overlay with the teammate’s base and our checkout. Review dependencies, bootstrap commands, checkpoints and media before use. Import necessary source and documentation selectively. Keep ZIPs, raw and exported media, model weights, local credentials, results, and bytecode out of the Git commit unless a separately reviewed exception applies.
3. **Validate teammate baseline.** Run the video and pipeline tests if present, verify the weather evaluation with the supplied split exclusion where supported, and check the documented Compose and UI flows. Capture output counts, relevant versions and any unavailable external services. Keep local image replay usable while original videos and GPS remain on Aarjav’s PC.
4. **Integrate Aarjav and Rishi’s vertical slice.** Verify a labeled fixture traverses legacy input or the v1 replay path, detector output, PostGIS merge, lifecycle and Grafana. Define the adapter against actual code first. Lack of reliable GPS in a legacy frame limits it to preview and evaluation. Establish capture-clock and GPS availability on a small PC sample before mapping original footage.
5. **Finish the semester scope.** Follow the existing labeling, model, and integration milestones with the verified baseline and data. Compare measured mapping and model outcomes with the acceptance criteria in [plan.md](plan.md).

## Branch workflow

`V2` is the integration branch. `feature/buffalo-handoff` starts from it and is the working branch for this integration. Validate changes there, merge or fast-forward them into `V2`, and publish only `V2` during development. Promote the validated integration branch to `main` at the end when the full acceptance criteria pass.
