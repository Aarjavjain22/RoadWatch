PYTHON ?= python3
SNAPSHOT ?= roadwatch_buffalo.db

.PHONY: db-init db-seed
db-init:
	psql -X --single-transaction --set ON_ERROR_STOP=1 --file db/schema_postgis.sql

db-seed:
	$(PYTHON) db/seed_postgis.py "$(SNAPSHOT)"
