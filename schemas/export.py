"""Regenerate the JSON Schemas for the five RoadWatch v1 topics."""

import json
from pathlib import Path

from schemas.events import TOPIC_SCHEMAS


if __name__ == "__main__":
    output = Path(__file__).with_name("json")
    output.mkdir(exist_ok=True)
    for topic, adapter in TOPIC_SCHEMAS.items():
        schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", **adapter.json_schema()}
        (output / f"{topic}.json").write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
