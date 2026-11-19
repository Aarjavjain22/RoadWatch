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

## Teammate source

The supplied overlay is preserved under `handoff/source_overlay/` pending the source ref required to run it. Its tests and runtime imports depend on legacy files outside the archive, including `streaming/contracts.py`. The overlay is not part of the active database bootstrap. See the [integration gates](handoff-integration.md) for the missing base files.
