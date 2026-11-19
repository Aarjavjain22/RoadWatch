PYTHON ?= python3
SNAPSHOT ?= roadwatch_buffalo.db

.PHONY: db-init db-seed
db-init:
	psql -X --single-transaction --set ON_ERROR_STOP=1 --file db/schema_postgis.sql

db-seed:
	$(PYTHON) db/seed_postgis.py "$(SNAPSHOT)"

build:
	docker compose build

up:
	docker compose up -d

spark:
	docker compose --profile spark up -d spark

ps:
	docker compose ps

logs:
	docker compose logs -f --tail=100

.PHONY: build up spark ps logs down ui demo-video
down:
	docker compose --profile spark down

ui:
	$(PYTHON) -m uvicorn app.main:app --host 127.0.0.1 --port 8080

demo-video:
	$(PYTHON) -m app.make_demo_video
