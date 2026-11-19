"""RoadWatch Weather & Context-Enrichment Agent (Phase 1).

An independent Kafka consumer — architecturally the same pattern as
`yolo_consumer.py` / `analytics_consumer.py` — that tags every frame with an
environmental-trust signal instead of leaving detections "weather-blind":

1. Context tagging: derive an image-based visibility heuristic (brightness,
   contrast, blur, glare) and look up real-world weather for the camera's
   location/timestamp from a free external API (Open-Meteo, no key required).
2. Combine both signals into a `visibility_condition`, a `confidence_adjustment`
   downstream consumers can apply to detection confidence, and an
   `escalation_flag` marking frames degraded enough to deserve a second look.
3. Emit an enriched `frame_weather_context` event per frame and persist it to
   PostgreSQL so Grafana can show a weather-stratified, always-on signal
   instead of a one-time manual check.

Phase 2/3 (VLM-based escalation review, automated condition reports, feeding
retraining) are intentionally out of scope here — see
`RoadWatch_Weather_Agent.pptx` / `docs/WEATHER_AGENT.md`.
"""
from __future__ import annotations

import base64
import json
import os
import time
from datetime import datetime, timezone

import cv2
import numpy as np
import psycopg2
import requests
from kafka import KafkaConsumer, KafkaProducer
from prometheus_client import Counter, Gauge, Histogram, start_http_server

from contracts import EVENT_VERSION, classify_visibility, classify_weather_code
from image_heuristics import compute_image_heuristics, crop_letterbox

KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "localhost:29092")
POSTGRES_DSN = os.getenv("POSTGRES_DSN", "postgresql://roadwatch:roadwatch@localhost:5432/roadwatch")
METRICS_PORT = int(os.getenv("METRICS_PORT", "8004"))

# Default location is Buffalo, NY (RoadWatch's target deployment region).
# Override per-camera once multi-camera geolocation is available.
WEATHER_LAT = float(os.getenv("WEATHER_LAT", "42.8864"))
WEATHER_LON = float(os.getenv("WEATHER_LON", "-78.8784"))
WEATHER_API_URL = os.getenv("WEATHER_API_URL", "https://api.open-meteo.com/v1/forecast")
WEATHER_CACHE_TTL_SEC = int(os.getenv("WEATHER_CACHE_TTL_SEC", "900"))  # 15 min buckets
WEATHER_TIMEOUT_SEC = float(os.getenv("WEATHER_TIMEOUT_SEC", "3.0"))
WEATHER_CLS_WEIGHTS = os.getenv("WEATHER_CLS_WEIGHTS", "./model/weights/weather_cls.pt")

RAW_TOPIC = "raw_frames"
CONTEXT_TOPIC = "frame_weather_context"
DLQ_TOPIC = "dead_letter"

FRAMES_TAGGED = Counter("weather_frames_tagged_total", "Frames enriched with weather/visibility context")
WEATHER_API_CALLS = Counter("weather_api_calls_total", "Calls made to the external weather API", ["result"])
VISIBILITY_TAGGED = Counter("weather_visibility_total", "Frames tagged per visibility condition", ["condition"])
ML_WEATHER_TAGGED = Counter("weather_ml_class_total", "Frames per weather-classifier top-1 class", ["weather_class"])
ESCALATIONS = Counter("weather_escalations_total", "Frames flagged for second-look escalation")
FAILED = Counter("weather_failed_frames_total", "Frames that failed context enrichment")
TAGGING_LATENCY = Histogram(
    "weather_tagging_latency_seconds",
    "Latency of per-frame weather/context enrichment",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)
AGENT_UP = Gauge("weather_agent_up", "1 if the weather/context-enrichment agent is running")

_weather_cache: dict[tuple[float, float, int], dict] = {}


def connect_pg():
    for _ in range(30):
        try:
            conn = psycopg2.connect(POSTGRES_DSN)
            conn.autocommit = True
            return conn
        except Exception as exc:
            print(f"[weather-agent] waiting for postgres: {exc}")
            time.sleep(2)
    raise SystemExit("could not connect to postgres")


def load_weather_classifier():
    """Load the trained weather classifier if its weights are present, else None."""
    if not os.path.exists(WEATHER_CLS_WEIGHTS):
        print(f"[weather-agent] no classifier at {WEATHER_CLS_WEIGHTS}; using heuristics + API only")
        return None
    from ultralytics import YOLO

    print(f"[weather-agent] loading weather classifier: {WEATHER_CLS_WEIGHTS}")
    return YOLO(WEATHER_CLS_WEIGHTS)


def classify_weather_image(classifier, image: np.ndarray) -> tuple[str | None, float]:
    if classifier is None:
        return None, 0.0
    probs = classifier.predict(image, verbose=False)[0].probs
    return classifier.names[int(probs.top1)], round(float(probs.top1conf), 4)


def fetch_weather(lat: float, lon: float) -> dict:
    """Fetch current weather for (lat, lon), cached per time bucket to be API-friendly.

    Never raises: on any failure the pipeline keeps flowing with an "unavailable"
    weather context rather than stalling frame processing on a flaky external API.
    """
    bucket = int(time.time() // WEATHER_CACHE_TTL_SEC)
    cache_key = (lat, lon, bucket)
    if cache_key in _weather_cache:
        return _weather_cache[cache_key]

    try:
        resp = requests.get(
            WEATHER_API_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,precipitation,cloud_cover,visibility,weather_code",
                "timezone": "UTC",
            },
            timeout=WEATHER_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        current = resp.json().get("current", {})
        weather_code = current.get("weather_code")
        result = {
            "temperature_c": current.get("temperature_2m"),
            "precipitation_mm": current.get("precipitation"),
            "cloud_cover_pct": current.get("cloud_cover"),
            "visibility_m": current.get("visibility"),
            "weather_code": weather_code,
            "weather_desc": classify_weather_code(weather_code),
            "source": "open-meteo",
        }
        WEATHER_API_CALLS.labels(result="ok").inc()
    except Exception as exc:
        print(f"[weather-agent][WARN] weather API unavailable: {exc}")
        result = {
            "temperature_c": None,
            "precipitation_mm": None,
            "cloud_cover_pct": None,
            "visibility_m": None,
            "weather_code": None,
            "weather_desc": "unknown",
            "source": "unavailable",
        }
        WEATHER_API_CALLS.labels(result="error").inc()

    _weather_cache[cache_key] = result
    if len(_weather_cache) > 64:  # keep the cache small; buckets rotate every TTL anyway
        _weather_cache.clear()
    return result


def main() -> None:
    start_http_server(METRICS_PORT)
    AGENT_UP.set(1)

    conn = connect_pg()
    cur = conn.cursor()
    classifier = load_weather_classifier()

    consumer = KafkaConsumer(
        RAW_TOPIC,
        bootstrap_servers=KAFKA_BOOTSTRAP,
        group_id="roadwatch-weather-agent",
        value_deserializer=lambda value: json.loads(value.decode("utf-8")),
        auto_offset_reset="earliest",
        enable_auto_commit=False,
        max_partition_fetch_bytes=5 * 1024 * 1024,
        fetch_max_bytes=5 * 1024 * 1024,
    )
    producer = KafkaProducer(
        bootstrap_servers=KAFKA_BOOTSTRAP,
        value_serializer=lambda value: json.dumps(value).encode("utf-8"),
        key_serializer=lambda key: key.encode("utf-8") if key else None,
        acks="all",
        retries=5,
        enable_idempotence=True,
    )

    print(f"[weather-agent] {RAW_TOPIC} -> {CONTEXT_TOPIC} (location={WEATHER_LAT},{WEATHER_LON})")

    for message in consumer:
        frame = message.value
        started = time.perf_counter()
        try:
            if frame.get("event_version") != EVENT_VERSION:
                raise ValueError(f"unsupported frame event_version={frame.get('event_version')!r}")

            img_bytes = base64.b64decode(frame["image_b64"])
            image = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError("could not decode frame image")

            image = crop_letterbox(image)
            heuristics = compute_image_heuristics(image)
            weather = fetch_weather(WEATHER_LAT, WEATHER_LON)
            ml_weather, ml_confidence = classify_weather_image(classifier, image)
            visibility_condition, confidence_adjustment, escalate = classify_visibility(
                heuristics["brightness"],
                heuristics["contrast"],
                heuristics["blur_score"],
                heuristics["glare_ratio"],
                weather["weather_desc"],
                ml_weather,
                ml_confidence,
            )

            context_event = {
                "event_version": EVENT_VERSION,
                "frame_id": frame["frame_id"],
                "trace_id": frame.get("trace_id", frame["frame_id"]),
                "camera_id": frame.get("camera_id", "dashcam_001"),
                "road_segment": frame.get("road_segment", "segment_00"),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "brightness": heuristics["brightness"],
                "contrast": heuristics["contrast"],
                "blur_score": heuristics["blur_score"],
                "glare_ratio": heuristics["glare_ratio"],
                "weather_temperature_c": weather["temperature_c"],
                "weather_precipitation_mm": weather["precipitation_mm"],
                "weather_cloud_cover_pct": weather["cloud_cover_pct"],
                "weather_visibility_m": weather["visibility_m"],
                "weather_description": weather["weather_desc"],
                "ml_weather_class": ml_weather,
                "ml_weather_confidence": ml_confidence if ml_weather else None,
                "visibility_condition": visibility_condition,
                "confidence_adjustment": confidence_adjustment,
                "escalation_flag": escalate,
            }
            producer.send(CONTEXT_TOPIC, key=context_event["frame_id"], value=context_event).get(timeout=30)

            cur.execute(
                """
                INSERT INTO frame_weather_context
                    (frame_id, trace_id, camera_id, road_segment, event_time,
                     brightness, contrast, blur_score, glare_ratio,
                     weather_temperature_c, weather_precipitation_mm, weather_cloud_cover_pct,
                     weather_visibility_m, weather_description,
                     ml_weather_class, ml_weather_confidence,
                     visibility_condition, confidence_adjustment, escalation_flag)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (frame_id) DO UPDATE SET
                    trace_id = EXCLUDED.trace_id,
                    camera_id = EXCLUDED.camera_id,
                    road_segment = EXCLUDED.road_segment,
                    event_time = EXCLUDED.event_time,
                    brightness = EXCLUDED.brightness,
                    contrast = EXCLUDED.contrast,
                    blur_score = EXCLUDED.blur_score,
                    glare_ratio = EXCLUDED.glare_ratio,
                    weather_temperature_c = EXCLUDED.weather_temperature_c,
                    weather_precipitation_mm = EXCLUDED.weather_precipitation_mm,
                    weather_cloud_cover_pct = EXCLUDED.weather_cloud_cover_pct,
                    weather_visibility_m = EXCLUDED.weather_visibility_m,
                    weather_description = EXCLUDED.weather_description,
                    ml_weather_class = EXCLUDED.ml_weather_class,
                    ml_weather_confidence = EXCLUDED.ml_weather_confidence,
                    visibility_condition = EXCLUDED.visibility_condition,
                    confidence_adjustment = EXCLUDED.confidence_adjustment,
                    escalation_flag = EXCLUDED.escalation_flag
                """,
                (
                    context_event["frame_id"], context_event["trace_id"], context_event["camera_id"],
                    context_event["road_segment"], context_event["timestamp"],
                    heuristics["brightness"], heuristics["contrast"],
                    heuristics["blur_score"], heuristics["glare_ratio"],
                    weather["temperature_c"], weather["precipitation_mm"], weather["cloud_cover_pct"],
                    weather["visibility_m"], weather["weather_desc"],
                    ml_weather, context_event["ml_weather_confidence"],
                    visibility_condition, confidence_adjustment, escalate,
                ),
            )

            VISIBILITY_TAGGED.labels(condition=visibility_condition).inc()
            if ml_weather:
                ML_WEATHER_TAGGED.labels(weather_class=ml_weather).inc()
            if escalate:
                ESCALATIONS.inc()
            FRAMES_TAGGED.inc()
            TAGGING_LATENCY.observe(time.perf_counter() - started)
            # The input is acknowledged only after the enriched event + row are durable.
            consumer.commit()

        except Exception as exc:
            FAILED.inc()
            dlq_record = {
                "event_version": EVENT_VERSION,
                "stage": "weather_context_enrichment",
                "source_topic": RAW_TOPIC,
                "source_id": frame.get("frame_id", "unknown"),
                "reason": str(exc),
                "failed_at": datetime.now(timezone.utc).isoformat(),
            }
            producer.send(DLQ_TOPIC, key=dlq_record["source_id"], value=dlq_record).get(timeout=30)
            consumer.commit()
            print(f"[weather-agent][ERROR] {frame.get('frame_id', 'unknown')}: {exc}")


if __name__ == "__main__":
    main()
