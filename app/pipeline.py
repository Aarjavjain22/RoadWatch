"""Read-only queries against the streaming pipeline's Postgres (Live monitor, Weather insights)."""
from __future__ import annotations

import psycopg2
import psycopg2.extras

from app.config import PG_DSN


class PipelineOffline(Exception):
    pass


def query(sql: str, params: tuple = ()) -> list[dict]:
    try:
        conn = psycopg2.connect(**PG_DSN)
    except psycopg2.OperationalError as exc:
        raise PipelineOffline(str(exc).strip().splitlines()[0]) from exc
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
    except psycopg2.errors.UndefinedTable as exc:
        raise PipelineOffline("pipeline tables missing — re-apply db/init.sql") from exc
    finally:
        conn.close()


def live_snapshot() -> dict:
    summary = query(
        """
        SELECT
          (SELECT COUNT(*) FROM detection_events)                                         AS total_detections,
          (SELECT COUNT(*) FROM detection_events WHERE event_time > NOW() - INTERVAL '10 minutes') AS recent_detections,
          (SELECT COUNT(DISTINCT frame_id) FROM frame_weather_context)                     AS frames_tagged,
          (SELECT ROUND(100.0 * AVG(escalation_flag::int), 1) FROM frame_weather_context
             WHERE event_time > NOW() - INTERVAL '1 hour')                                AS escalated_pct,
          (SELECT MAX(event_time) FROM detection_events)                                  AS last_detection_at
        """
    )[0]
    weather = query(
        """
        SELECT
          d.frame_id,
          d.event_time AS context_as_of,
          w.weather_description,
          w.weather_temperature_c,
          w.weather_precipitation_mm,
          w.weather_visibility_m,
          w.visibility_condition,
          w.ml_weather_class,
          w.ml_weather_confidence,
          w.event_time AS tagged_at
        FROM detection_events d
        LEFT JOIN frame_weather_context w ON w.frame_id = d.frame_id
        ORDER BY d.event_time DESC
        LIMIT 1
        """
    )
    recent = query(
        """
        SELECT event_id, frame_id, event_time, source_image, road_segment, damage_type,
               confidence, severity, bbox, visibility_condition, adjusted_confidence, escalation_flag,
               ml_weather_class
        FROM detection_weather ORDER BY event_time DESC LIMIT 40
        """
    )
    return {"summary": summary, "weather": weather[0] if weather else None, "recent": recent}


def weather_insights() -> dict:
    by_condition = query(
        """
        SELECT COALESCE(visibility_condition, 'untagged') AS condition,
               COUNT(*) AS detections,
               ROUND(AVG(confidence)::numeric, 3) AS avg_confidence,
               ROUND(AVG(adjusted_confidence)::numeric, 3) AS avg_adjusted,
               ROUND(100.0 * AVG(COALESCE(escalation_flag, false)::int), 1) AS escalated_pct
        FROM detection_weather GROUP BY 1 ORDER BY detections DESC
        """
    )
    by_type = query(
        """
        SELECT damage_type,
               COUNT(*) FILTER (WHERE visibility_condition = 'clear') AS clear_n,
               ROUND(AVG(confidence) FILTER (WHERE visibility_condition = 'clear')::numeric, 3) AS clear_conf,
               COUNT(*) FILTER (WHERE visibility_condition <> 'clear') AS degraded_n,
               ROUND(AVG(confidence) FILTER (WHERE visibility_condition <> 'clear')::numeric, 3) AS degraded_conf
        FROM detection_weather GROUP BY 1 ORDER BY 1
        """
    )
    ml_classes = query(
        """
        SELECT ml_weather_class AS weather_class, COUNT(*) AS frames,
               ROUND(AVG(ml_weather_confidence)::numeric, 3) AS avg_confidence
        FROM frame_weather_context WHERE ml_weather_class IS NOT NULL
        GROUP BY 1 ORDER BY frames DESC
        """
    )
    validation = query(
        "SELECT * FROM weather_ground_truth_validation ORDER BY observed_date DESC LIMIT 7"
    )
    return {"by_condition": by_condition, "by_type": by_type,
            "ml_classes": ml_classes, "validation": validation}
