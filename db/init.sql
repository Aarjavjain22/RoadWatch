CREATE TABLE IF NOT EXISTS detection_events (
    event_version   TEXT NOT NULL,
    event_id        TEXT PRIMARY KEY,
    trace_id        TEXT NOT NULL,
    frame_id        TEXT NOT NULL,
    camera_id       TEXT,
    event_time      TIMESTAMPTZ NOT NULL,
    source_image    TEXT,
    road_segment    TEXT NOT NULL,
    damage_type     TEXT NOT NULL,
    confidence      DOUBLE PRECISION NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    severity        TEXT,
    latency_ms      DOUBLE PRECISION,
    ingested_at     TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_events_segment_time ON detection_events (road_segment, event_time DESC);
CREATE INDEX IF NOT EXISTS idx_events_type_time ON detection_events (damage_type, event_time DESC);
CREATE INDEX IF NOT EXISTS idx_events_trace ON detection_events (trace_id);

CREATE TABLE IF NOT EXISTS segment_window_agg (
    window_start    TIMESTAMPTZ,
    window_end      TIMESTAMPTZ,
    road_segment    TEXT,
    damage_type     TEXT,
    event_count     BIGINT,
    avg_confidence  DOUBLE PRECISION,
    PRIMARY KEY (window_start, road_segment, damage_type)
);

-- Weather & Context-Enrichment Agent (Phase 1): one row per source frame,
-- tagging it with image-based visibility heuristics + external weather context.
CREATE TABLE IF NOT EXISTS frame_weather_context (
    frame_id                    TEXT PRIMARY KEY,
    trace_id                    TEXT NOT NULL,
    camera_id                   TEXT,
    road_segment                TEXT NOT NULL,
    event_time                  TIMESTAMPTZ NOT NULL,
    brightness                  DOUBLE PRECISION,
    contrast                    DOUBLE PRECISION,
    blur_score                  DOUBLE PRECISION,
    glare_ratio                 DOUBLE PRECISION,
    weather_temperature_c       DOUBLE PRECISION,
    weather_precipitation_mm    DOUBLE PRECISION,
    weather_cloud_cover_pct     DOUBLE PRECISION,
    weather_visibility_m        DOUBLE PRECISION,
    weather_description         TEXT,
    visibility_condition        TEXT NOT NULL,
    confidence_adjustment       DOUBLE PRECISION NOT NULL CHECK (confidence_adjustment > 0 AND confidence_adjustment <= 1),
    escalation_flag             BOOLEAN NOT NULL DEFAULT FALSE,
    ingested_at                 TIMESTAMPTZ DEFAULT now()
);

-- Added after the first release; ADD COLUMN IF NOT EXISTS keeps re-running this file safe on existing volumes.
ALTER TABLE frame_weather_context ADD COLUMN IF NOT EXISTS ml_weather_class TEXT;
ALTER TABLE frame_weather_context ADD COLUMN IF NOT EXISTS ml_weather_confidence DOUBLE PRECISION;
ALTER TABLE detection_events ADD COLUMN IF NOT EXISTS bbox DOUBLE PRECISION[];

CREATE INDEX IF NOT EXISTS idx_weather_context_segment_time ON frame_weather_context (road_segment, event_time DESC);
CREATE INDEX IF NOT EXISTS idx_weather_context_condition ON frame_weather_context (visibility_condition);
CREATE INDEX IF NOT EXISTS idx_weather_context_trace ON frame_weather_context (trace_id);

-- Ground-truth validation: one row per calendar day, comparing the Weather Agent's
-- own tagging against an independent NOAA NCEI observation for Buffalo, NY.
CREATE TABLE IF NOT EXISTS weather_ground_truth_validation (
    observed_date                       DATE PRIMARY KEY,
    noaa_observed_date                  DATE NOT NULL,
    is_exact_date_match                 BOOLEAN NOT NULL DEFAULT TRUE,
    noaa_station_id                     TEXT NOT NULL,
    noaa_tmax_c                         DOUBLE PRECISION,
    noaa_tmin_c                         DOUBLE PRECISION,
    noaa_precip_mm                      DOUBLE PRECISION,
    noaa_snow_mm                        DOUBLE PRECISION,
    noaa_snow_depth_mm                  DOUBLE PRECISION,
    noaa_condition                      TEXT NOT NULL,
    agent_frames_total                  BIGINT NOT NULL DEFAULT 0,
    agent_frames_adverse                BIGINT NOT NULL DEFAULT 0,
    agent_adverse_pct                   DOUBLE PRECISION NOT NULL DEFAULT 0,
    agent_avg_confidence_adjustment     DOUBLE PRECISION,
    agent_escalations                   BIGINT NOT NULL DEFAULT 0,
    agreement                           BOOLEAN NOT NULL,
    agreement_note                      TEXT,
    checked_at                          TIMESTAMPTZ NOT NULL
);

-- Detection x weather context: every detection with the environmental verdict for its source frame.
CREATE OR REPLACE VIEW detection_weather AS
SELECT
    d.event_id,
    d.frame_id,
    d.event_time,
    d.source_image,
    d.road_segment,
    d.damage_type,
    d.confidence,
    d.severity,
    d.bbox,
    w.visibility_condition,
    w.confidence_adjustment,
    ROUND((d.confidence * COALESCE(w.confidence_adjustment, 1))::numeric, 4) AS adjusted_confidence,
    w.escalation_flag,
    w.weather_description,
    w.ml_weather_class,
    w.ml_weather_confidence
FROM detection_events d
LEFT JOIN frame_weather_context w ON w.frame_id = d.frame_id;
