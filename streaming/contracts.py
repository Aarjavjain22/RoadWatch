"""Dependency-free RoadWatch event-contract utilities."""
from __future__ import annotations

import os
import re

EVENT_VERSION = "1.0"
CLASS_NAMES = ["Longitudinal", "Transverse", "Alligator", "Pothole"]
REQUIRED_FIELDS = [
    "event_version", "event_id", "frame_id", "trace_id",
    "damage_type", "confidence", "road_segment",
]


def severity_from_area(area_ratio: float, conf: float) -> str:
    if area_ratio > 0.03 or conf > 0.7:
        return "High"
    if area_ratio > 0.01 or conf > 0.45:
        return "Medium"
    return "Low"


def video_slug(video_name: str) -> str:
    stem = os.path.splitext(os.path.basename(video_name))[0]
    return re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_") or "video"


def video_frame_id(video_name: str, offset_ms: int) -> str:
    """Stable id for a sampled video frame: the same clip position always maps to the same id,
    so replay passes upsert rather than duplicate, and weather/detection rows join on it."""
    return f"{video_slug(video_name)}_{int(offset_ms):08d}"


def video_road_segment(video_name: str) -> str:
    match = re.search(r"\d+", os.path.splitext(os.path.basename(video_name))[0])
    return f"buffalo_v{int(match.group()):02d}" if match else "buffalo"


def validation_error(event: dict) -> str | None:
    for field in REQUIRED_FIELDS:
        if field not in event or event[field] in (None, ""):
            return "schema_invalid"

    if event.get("event_version") != EVENT_VERSION:
        return "unsupported_event_version"

    confidence = event.get("confidence")
    if not isinstance(confidence, (int, float)) or not (0.0 <= confidence <= 1.0):
        return "confidence_out_of_range"

    if event.get("damage_type") not in CLASS_NAMES:
        return "unknown_damage_type"

    return None


# ---------------------------------------------------------------------------
# Weather & Context-Enrichment Agent (Phase 1) — pure, dependency-free helpers.
#
# Image heuristics (brightness/contrast/blur/glare) are computed with OpenCV in
# `weather_agent.py` so this module stays import-light; the classification logic
# below only deals with plain numbers/strings and is unit-testable in isolation.
# ---------------------------------------------------------------------------

# WMO weather interpretation codes, as returned by the Open-Meteo API.
WMO_WEATHER_CODES = {
    0: "clear", 1: "mainly_clear", 2: "partly_cloudy", 3: "overcast",
    45: "fog", 48: "depositing_rime_fog",
    51: "light_drizzle", 53: "drizzle", 55: "dense_drizzle",
    61: "light_rain", 63: "rain", 65: "heavy_rain",
    71: "light_snow", 73: "snow", 75: "heavy_snow", 77: "snow_grains",
    80: "rain_showers", 81: "heavy_rain_showers", 82: "violent_rain_showers",
    85: "snow_showers", 86: "heavy_snow_showers",
    95: "thunderstorm", 96: "thunderstorm_hail", 99: "thunderstorm_heavy_hail",
}

# Weather descriptions that RoadWatch treats as visibility-degrading for detection trust.
ADVERSE_WEATHER = {
    "fog", "depositing_rime_fog",
    "light_drizzle", "drizzle", "dense_drizzle",
    "light_rain", "rain", "heavy_rain",
    "light_snow", "snow", "heavy_snow", "snow_grains",
    "rain_showers", "heavy_rain_showers", "violent_rain_showers",
    "snow_showers", "heavy_snow_showers",
    "thunderstorm", "thunderstorm_hail", "thunderstorm_heavy_hail",
}


def classify_weather_code(code) -> str:
    """Map an Open-Meteo WMO weather code to a stable text label."""
    if code is None:
        return "unknown"
    try:
        return WMO_WEATHER_CODES.get(int(code), "unknown")
    except (TypeError, ValueError):
        return "unknown"


# Classes predicted by the trained weather classifier (model/prepare_weather_cls.py).
ML_WEATHER_CLASSES = ["clear", "cloudy", "fog", "rain", "snow"]
ML_ADVERSE_WEATHER = {"fog", "rain", "snow"}
ML_CONFIDENCE_MIN = 0.6
LOW_LIGHT_BRIGHTNESS = 60


def classify_visibility(
    brightness: float,
    contrast: float,
    blur_score: float,
    glare_ratio: float,
    weather_desc: str,
    ml_weather: str | None = None,
    ml_confidence: float = 0.0,
) -> tuple[str, float, bool]:
    """Combine per-frame image heuristics with external weather into one verdict.

    Returns ``(visibility_condition, confidence_adjustment, escalation_flag)``:

    - ``visibility_condition``: ``"clear"`` or a ``+``-joined list of degrading factors
      (e.g. ``"low_light+snow"``).
    - ``confidence_adjustment``: multiplicative factor in ``(0, 1]`` downstream
      consumers can apply to a detection's confidence to reflect how trustworthy
      the source frame was.
    - ``escalation_flag``: ``True`` when the frame is degraded enough that any
      detection drawn from it deserves a second look (e.g. VLM escalation).

    ``ml_weather`` / ``ml_confidence`` are the image classifier's top-1 class and
    probability; they only count as a degrading factor at ``ML_CONFIDENCE_MIN``
    or above, because fog/snow have very few training examples, and never on
    low-light frames: the classifier was trained on daytime images only (98.5%
    of them brighter than ``LOW_LIGHT_BRIGHTNESS``), so at night its output is noise.
    """
    reasons: list[str] = []
    adjustment = 1.0

    low_light = brightness < LOW_LIGHT_BRIGHTNESS
    if low_light:
        reasons.append("low_light")
        adjustment *= 0.80
    if contrast < 25:
        reasons.append("low_contrast")
        adjustment *= 0.90
    if blur_score < 80:
        reasons.append("blurry")
        adjustment *= 0.80
    if glare_ratio > 0.08:
        reasons.append("glare")
        adjustment *= 0.85
    if weather_desc in ADVERSE_WEATHER:
        reasons.append(weather_desc)
        adjustment *= 0.75
    if not low_light and ml_weather in ML_ADVERSE_WEATHER and ml_confidence >= ML_CONFIDENCE_MIN:
        reasons.append(f"ml_{ml_weather}")
        adjustment *= 0.80

    condition = "clear" if not reasons else "+".join(reasons)
    adjustment = round(max(adjustment, 0.3), 3)
    escalate = adjustment <= 0.65 or len(reasons) >= 2
    return condition, adjustment, escalate


# ---------------------------------------------------------------------------
# Ground-truth validation against NOAA NCEI daily summaries (Buffalo, NY).
#
# This closes the loop proposed in the dataset-diversity plan: instead of only
# trusting the agent's own heuristic + forecast-API tag, periodically compare
# it against an independent, authoritative observation for the same day.
# ---------------------------------------------------------------------------

def classify_noaa_conditions(precip_mm: float | None, snow_mm: float | None) -> str:
    """Derive a coarse ground-truth label from a NOAA GHCN daily summary.

    `precip_mm` / `snow_mm` are the `PRCP` / `SNOW` fields from NOAA's
    `daily-summaries` dataset (metric units). Missing values are treated as 0.
    """
    precip_mm = precip_mm or 0.0
    snow_mm = snow_mm or 0.0

    if snow_mm > 0 and precip_mm > 0:
        return "snow+rain"
    if snow_mm > 0:
        return "snow"
    if precip_mm > 0:
        return "rain"
    return "clear"


def ground_truth_agreement(noaa_condition: str, agent_adverse_pct: float) -> tuple[bool, str]:
    """Score whether the agent's frame-level adverse-weather rate matches NOAA.

    `agent_adverse_pct` is the fraction (0..1) of frames the agent tagged with a
    non-"clear" `visibility_condition` for the same day. Returns
    ``(agrees, note)``; this is intentionally coarse (daily granularity, no
    per-frame NOAA ground truth exists) and meant as a monitoring signal, not a
    hard pass/fail test.
    """
    noaa_adverse = noaa_condition != "clear"
    agent_adverse = agent_adverse_pct >= 0.3

    if noaa_adverse == agent_adverse:
        return True, "agent adverse-weather rate matches NOAA-reported conditions"
    if noaa_adverse and not agent_adverse:
        return False, "NOAA reports precipitation/snow but agent tagged mostly clear frames"
    return False, "agent tagged many degraded frames but NOAA reports clear conditions"
