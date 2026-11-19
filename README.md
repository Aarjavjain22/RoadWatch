# RoadWatch Buffalo

See [the semester plan](plan.md) and [the handoff review](handoff-integration.md) for project scope and integration status.

## Initialize the Buffalo database

Use a fresh PostgreSQL database with PostGIS available (validated with PostgreSQL 16 and PostGIS 3.5). Set libpq connection variables such as `PGHOST`, `PGPORT`, `PGDATABASE`, and `PGUSER`. Supply authentication through your local libpq configuration.

```sh
python -m pip install -r db/requirements.txt
make db-init
make db-seed SNAPSHOT=/path/to/roadwatch_buffalo.db
```

Run `db-init` once per empty database. The seed imports the seven public tables and their provenance and quality checks in one transaction. A rerun skips existing keys. Use a fresh database for a different snapshot. The loader interprets unzoned Buffalo and NYS timestamps as America/New_York local times and converts year-only coverage bounds to the first and last days of their respective years.

Use the supplied SQLite snapshot, or run `db/build_database.py` from a scratch directory to fetch a new snapshot. The builder replaces any file named `roadwatch_buffalo.db` in its working directory.

## Run the teammate’s baseline

The source from the platform and UI bundles is integrated under `streaming/`, `app/`, `model/`, and `observability/`. Place supplied checkpoints in `model/weights/` and use an approved directory of JPEGs for the producer. Copy `.env.example` to `.env` and set `FRAMES_HOST_DIR` for image replay. For GoPro video mode, set `PRODUCER_SOURCE=video` and stage the originals at the video path declared in Compose.

```sh
docker compose build
docker compose up -d
python -m pip install -r app/requirements.txt -r schemas/requirements.txt pytest
python -m pytest -q tests
make ui
```

Compose initializes the legacy database tables automatically on a new volume. Apply the Buffalo schema separately using the database steps above. The local UI uses the same Postgres host port as Compose through `.env`. For a single pass over an image directory, use `docker compose run --rm producer python producer.py` while the consumer services are running.

The build uses the standard certificate trust store. Where a managed environment requires an additional CA, the streaming Dockerfile accepts an optional BuildKit secret called `pip_ca` containing the approved CA bundle. The certificate is available only during dependency installation.

## Event Format v1

[The contract](event-contract.md) defines the five `roadwatch.*` topics alongside the preserved legacy topics. The models live in `schemas/events.py`. Regenerate the versioned JSON schemas with `python -m schemas.export`. Topic provisioning and payload validation are available. Buffalo GPS replay and hazard mapping use the v1 contract. The seven-class ONNX worker is a separate milestone.

## Buffalo extension on an Intel Mac

**Meeting file:** Open [RoadWatch_Buffalo_Demo.html](RoadWatch_Buffalo_Demo.html) directly in Chrome. It includes the public Buffalo road/311 baseline, coarse previews from the supplied Buffalo evaluation set, and its archived detector boxes. Video locations follow an illustrative Main Street route because the supplied frame archive has no matching GPS or recording clock. A separate simulated scenario demonstrates confirmation, resolution, and a 311 draft. Everything needed to display the file is embedded.

### Run on local files

Use Python 3.11 or newer from the repository root. SQLite ships with Python. This path has no Docker, Kafka, or PostgreSQL dependency.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r ingestion/requirements.txt -r schemas/requirements.txt
mkdir -p demo-output
```

Keep videos, extracted images, GPS logs, and generated event files under an ignored directory such as `data/` or outside the repository. Use privacy-reviewed extracted frames when enabling previews or Kafka. Provide one JSON manifest per independent physical drive. GPS and frame offsets share the video timeline. An example manifest for files in a frames directory follows.

```json
{
  "drive_id": "buffalo-01",
  "vehicle_id": "camera-car-1",
  "started_at": "2025-10-06T14:00:00Z",
  "privacy_reviewed": true,
  "route_name": "Main Street",
  "frames": [
    {"image": "frame-000.jpg", "offset_s": 0},
    {"image": "frame-001.jpg", "offset_s": 5},
    {"image": "frame-002.jpg", "offset_s": 10}
  ],
  "gps": [
    {"offset_s": 0, "lat": 42.9081, "lon": -78.8670, "heading_deg": 21, "accuracy_m": 6},
    {"offset_s": 10, "lat": 42.9085, "lon": -78.8669, "heading_deg": 25, "accuracy_m": 6}
  ],
  "route_points": [[42.9081, -78.8670], [42.9085, -78.8669]]
}
```

Set `privacy_reviewed` only after checking the images for faces and plates. Frame and GPS rows accept an ISO timestamp with a UTC offset in place of `offset_s`. Align the two clocks or provide an explicit shared capture-time anchor before ingesting field drives. A `gps_csv` relative to the manifest can replace the inline track, with `offset_s` or `ts`, `lat`, `lon`, and optional `heading_deg`, `speed_kph`, `accuracy_m`. Missing frame offsets are interpolated from neighboring anchors or assigned with `frame_interval_s` (default 1). GPS interpolates only between samples at most `max_gps_gap_s` apart (default 15). Outside the GPS span, supplied `route_points` are used as an approximate route, otherwise the frame has no position. With no absolute time anchor, the event uses relative time and sets `clock_known: false`.

```sh
DB=demo-output/buffalo.db
BASELINE=/path/to/roadwatch_buffalo.db
python -m ingestion.ingest_drive --db "$DB" --baseline "$BASELINE" \
  --manifest data/drive-01.json --frames-dir data/drive-01-frames
python -m ingestion.replay_drive --db "$DB" --drive buffalo-01 \
  --jsonl demo-output/frames.jsonl
python -m mapping.hazard_mapper --db "$DB" --baseline "$BASELINE" \
  --detections data/detections.jsonl --draft-311
python -m mapping.export_dashboard --db "$DB" --baseline "$BASELINE" \
  --json demo-output/hazards.json --html demo-output/hazards.html --thumbnails
```

The mapper accepts Event Format v1 `DetectionBatch` JSONL, legacy detections JSONL, or the teammate’s evaluation CSV. Join by `frame_id`. Set a manifest frame’s optional `frame_id` when using previously computed detections. Use one detector/model per workspace. The legacy stream and CSV contain positive sightings only. Resolution requires explicit successful, clean v1 frame results.

Pins merge within 12 m on the same road segment and class. Set `--radius-m`, `--confirm-drives`, and `--resolve-drives` to change the defaults. Two independent GPS-backed drives with known capture times confirm by default. Resolution needs two later clean crossings in the same direction with clean positions on both sides of the pin. Route estimates and relative timestamps are excluded from lifecycle evidence. Estimated hazard coordinates use the snapped camera position unless a detection supplies a calibrated `distance_m` and the frame has heading. The camera proxy stores additional uncertainty. `--draft-311` prepares local pothole drafts for confirmed pins and links a nearby open pothole case when available. Drafts have no submission path.

### Optional Kafka and PostGIS

Install `streaming/requirements.txt` for the Kafka client. Supply `--kafka localhost:29092 --rate 4` on the replay command for paced streaming. Add `--legacy` to also send image bytes on `raw_frames` for the existing detector. Image refs in v1 refer to shared frame storage. Kafka publishing failure leaves the JSONL replay available. The mapper accepts `--kafka localhost:29092` with `--topic roadwatch.detections` (the default) or `--topic road_damage_events` for the legacy detector. Use a Kafka group per local workspace. The mapper commits Kafka offsets after durable persistence.

For native PostgreSQL/PostGIS, apply the Buffalo schema and seed as above, then pass `--postgis env` to ingestion or mapping, with your `PG*` connection settings. The selected SQLite workspace remains the source of evidence. The optional sync applies `db/buffalo_runtime.sql` and mirrors drives, GPS, frames, detections, pins and drafts transactionally to PostGIS. Use distinct drive IDs across workspaces. Omit `--postgis` for a fully local run or if PostGIS is unreachable.

### Regenerate the bundled meeting demo

```sh
python -m mapping.build_demo --baseline "$BASELINE" \
  --frames-dir data/buffalo_eval_frames \
  --detections results/buffalo_detections.csv \
  --html RoadWatch_Buffalo_Demo.html
```

Install `numpy`, `opencv-python-headless`, and `pytest` alongside the ingestion and schema requirements to run `python -m pytest -q`. The unit and local integration tests run without broker or database services.
