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

[The contract](event-contract.md) defines the five `roadwatch.*` topics alongside the preserved legacy topics. The models live in `schemas/events.py`. Regenerate the versioned JSON schemas with `python -m schemas.export`. Topic provisioning and payload validation are available. The Buffalo GPS replay, mapper, and ONNX worker are future integration milestones.
