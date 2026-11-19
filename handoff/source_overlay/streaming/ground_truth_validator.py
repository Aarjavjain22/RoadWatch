"""RoadWatch Weather Ground-Truth Validator.

Closes the loop called for in the dataset-diversity plan's "validate
geographically" step: instead of only trusting the Weather & Context Agent's
own heuristic + forecast-API tag, periodically cross-check it against an
independent, authoritative observation for the same calendar day — NOAA's
free, unauthenticated NCEI Access Data Service (GHCN daily summaries) for the
Buffalo Niagara International Airport station (`USW00014733`).

This is NOT a Kafka consumer: NOAA daily summaries only change once per day,
so this runs on a simple timer loop, reads the Weather Agent's own output
(`frame_weather_context`) back out of PostgreSQL, aggregates it for "today",
and writes one row per day into `weather_ground_truth_validation`.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import psycopg2
import requests
from prometheus_client import Counter, Gauge, start_http_server

from contracts import classify_noaa_conditions, ground_truth_agreement

POSTGRES_DSN = os.getenv("POSTGRES_DSN", "postgresql://roadwatch:roadwatch@localhost:5432/roadwatch")
METRICS_PORT = int(os.getenv("METRICS_PORT", "8005"))

NOAA_STATION_ID = os.getenv("NOAA_STATION_ID", "USW00014733")  # Buffalo Niagara Int'l Airport
NOAA_API_URL = os.getenv("NOAA_API_URL", "https://www.ncei.noaa.gov/access/services/data/v1")
NOAA_TIMEOUT_SEC = float(os.getenv("NOAA_TIMEOUT_SEC", "10.0"))
VALIDATION_INTERVAL_SEC = int(os.getenv("VALIDATION_INTERVAL_SEC", "120"))

NOAA_API_CALLS = Counter("noaa_api_calls_total", "Calls made to the NOAA NCEI ground-truth API", ["result"])
VALIDATION_RUNS = Counter("ground_truth_validation_runs_total", "Validation cycles completed")
AGREEMENT_GAUGE = Gauge("ground_truth_agreement", "1 if today's agent tagging agrees with NOAA ground truth, else 0")
VALIDATOR_UP = Gauge("ground_truth_validator_up", "1 if the ground-truth validator is running")


def connect_pg():
    for _ in range(30):
        try:
            conn = psycopg2.connect(POSTGRES_DSN)
            conn.autocommit = True
            return conn
        except Exception as exc:
            print(f"[ground-truth] waiting for postgres: {exc}")
            time.sleep(2)
    raise SystemExit("could not connect to postgres")


def fetch_noaa_today(station_id: str) -> dict | None:
    """Fetch the most recent available NOAA daily summary for `station_id`.

    GHCN daily summaries lag by a few days while QA checks complete, so this
    pulls the last 7 days and uses the most recent row rather than assuming
    "today" is already published. Returns None on any failure.
    """
    today = datetime.now(timezone.utc).date()
    start = today.replace(day=max(1, today.day - 7)) if today.day > 7 else today
    try:
        resp = requests.get(
            NOAA_API_URL,
            params={
                "dataset": "daily-summaries",
                "stations": station_id,
                "startDate": start.isoformat(),
                "endDate": today.isoformat(),
                "dataTypes": "TMAX,TMIN,PRCP,SNOW,SNWD",
                "units": "metric",
                "format": "json",
            },
            timeout=NOAA_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            raise ValueError("no rows returned")
        latest = rows[-1]
        NOAA_API_CALLS.labels(result="ok").inc()
        return {
            "observed_date": latest.get("DATE"),
            "tmax_c": _to_float(latest.get("TMAX")),
            "tmin_c": _to_float(latest.get("TMIN")),
            "precip_mm": _to_float(latest.get("PRCP")),
            "snow_mm": _to_float(latest.get("SNOW")),
            "snow_depth_mm": _to_float(latest.get("SNWD")),
        }
    except Exception as exc:
        print(f"[ground-truth][WARN] NOAA API unavailable: {exc}")
        NOAA_API_CALLS.labels(result="error").inc()
        return None


def _to_float(value) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def run_validation_cycle(cur) -> None:
    """Compare today's agent tagging against the most recently published NOAA summary.

    NOAA GHCN daily summaries lag real time by a few days while QA checks
    complete, so the latest available NOAA observation is almost always for an
    earlier date than "today" (when this replay pipeline's frames are
    stamped). Rather than silently comparing the agent's today-stats against a
    zero-frame historical date, this treats NOAA's latest reading as the best
    available proxy for current conditions, compares it against *today's*
    agent frames, and records `is_exact_date_match` so the gap is visible
    instead of hidden.
    """
    noaa = fetch_noaa_today(NOAA_STATION_ID)
    if noaa is None:
        return

    today = datetime.now(timezone.utc).date()
    is_exact_date_match = noaa["observed_date"] == today.isoformat()

    cur.execute(
        """
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE visibility_condition <> 'clear') AS adverse,
            AVG(confidence_adjustment) AS avg_adjustment,
            COUNT(*) FILTER (WHERE escalation_flag) AS escalations
        FROM frame_weather_context
        WHERE event_time::date = %s
        """,
        (today.isoformat(),),
    )
    total, adverse, avg_adjustment, escalations = cur.fetchone()
    total = total or 0
    adverse = adverse or 0
    agent_adverse_pct = (adverse / total) if total else 0.0

    noaa_condition = classify_noaa_conditions(noaa["precip_mm"], noaa["snow_mm"])
    agrees, note = ground_truth_agreement(noaa_condition, agent_adverse_pct)
    if not is_exact_date_match:
        note += f" (NOAA's latest published reading is for {noaa['observed_date']}, used as a proxy for today)"

    cur.execute(
        """
        INSERT INTO weather_ground_truth_validation
            (observed_date, noaa_observed_date, is_exact_date_match,
             noaa_station_id, noaa_tmax_c, noaa_tmin_c,
             noaa_precip_mm, noaa_snow_mm, noaa_snow_depth_mm, noaa_condition,
             agent_frames_total, agent_frames_adverse, agent_adverse_pct,
             agent_avg_confidence_adjustment, agent_escalations,
             agreement, agreement_note, checked_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (observed_date) DO UPDATE SET
            noaa_observed_date = EXCLUDED.noaa_observed_date,
            is_exact_date_match = EXCLUDED.is_exact_date_match,
            noaa_tmax_c = EXCLUDED.noaa_tmax_c,
            noaa_tmin_c = EXCLUDED.noaa_tmin_c,
            noaa_precip_mm = EXCLUDED.noaa_precip_mm,
            noaa_snow_mm = EXCLUDED.noaa_snow_mm,
            noaa_snow_depth_mm = EXCLUDED.noaa_snow_depth_mm,
            noaa_condition = EXCLUDED.noaa_condition,
            agent_frames_total = EXCLUDED.agent_frames_total,
            agent_frames_adverse = EXCLUDED.agent_frames_adverse,
            agent_adverse_pct = EXCLUDED.agent_adverse_pct,
            agent_avg_confidence_adjustment = EXCLUDED.agent_avg_confidence_adjustment,
            agent_escalations = EXCLUDED.agent_escalations,
            agreement = EXCLUDED.agreement,
            agreement_note = EXCLUDED.agreement_note,
            checked_at = EXCLUDED.checked_at
        """,
        (
            today.isoformat(), noaa["observed_date"], is_exact_date_match,
            NOAA_STATION_ID, noaa["tmax_c"], noaa["tmin_c"],
            noaa["precip_mm"], noaa["snow_mm"], noaa["snow_depth_mm"], noaa_condition,
            total, adverse, round(agent_adverse_pct, 4),
            float(avg_adjustment) if avg_adjustment is not None else None, escalations or 0,
            agrees, note, datetime.now(timezone.utc).isoformat(),
        ),
    )

    AGREEMENT_GAUGE.set(1 if agrees else 0)
    VALIDATION_RUNS.inc()
    print(
        f"[ground-truth] today={today} vs NOAA({noaa['observed_date']})={noaa_condition} "
        f"(precip={noaa['precip_mm']}mm, snow={noaa['snow_mm']}mm) vs agent "
        f"adverse_pct={agent_adverse_pct:.2f} over {total} frames -> "
        f"agreement={agrees} ({note})"
    )


def main() -> None:
    start_http_server(METRICS_PORT)
    VALIDATOR_UP.set(1)

    conn = connect_pg()
    cur = conn.cursor()

    print(
        f"[ground-truth] validating against NOAA station {NOAA_STATION_ID} "
        f"every {VALIDATION_INTERVAL_SEC}s"
    )

    while True:
        try:
            run_validation_cycle(cur)
        except Exception as exc:
            print(f"[ground-truth][ERROR] validation cycle failed: {exc}")
        time.sleep(VALIDATION_INTERVAL_SEC)


if __name__ == "__main__":
    main()
