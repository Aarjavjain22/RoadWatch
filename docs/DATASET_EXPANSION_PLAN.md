# Dataset & Ground-Truth Expansion Plan

This captures the weather-diversity dataset research for the Weather &
Context-Enrichment Agent (see [`RoadWatch_Weather_Agent.pptx`](../RoadWatch_Weather_Agent.pptx)
and [`docs/WEATHER_AGENT.md`](WEATHER_AGENT.md)), what it means, and what of it
is actually implemented versus deferred as a documented backlog.

## The four tracks, and why each matters

1. **Pavement/crack datasets with weather diversity** (RDD2022 full/Norway, CrackNet-Weather, CarNet) — the most directly relevant upgrade path, since it's the *same task* (crack/pothole detection) under adverse conditions, in some cases the *same label format* RoadWatch already consumes.
2. **General adverse-weather driving datasets** (ACDC, Ithaca365, CADC, BDD100K, DAWN) — not pavement-labeled, but useful for pretraining a weather-robust backbone, or for training/validating a standalone weather classifier. **Ithaca365** and **CADC** are the strongest citations for "this generalizes to Buffalo/our platform," since they're geographically/platform-analogous (upstate-NY snow route; modified Lincoln vehicle).
3. **Lightweight weather-classification datasets** (MWD, 5-Class Kaggle) — purpose-built to train the "context tagging" step (Slide 4, Step 1 of the proposal) as a learned classifier instead of/alongside hand-tuned image heuristics.
4. **Buffalo-specific live sources** (511NY API, Thruway cameras, NOAA NCEI) — for real ground truth and eventual live testing. This is the only track that's free, immediately accessible, and doesn't require downloading/training anything — so it's the one implemented first.

## What's implemented now

| Item | Where | Notes |
|---|---|---|
| Image-heuristic + live-weather-API context tagging | `streaming/weather_agent.py`, `streaming/contracts.py` | Phase 1, already running (see `docs/WEATHER_AGENT.md`) |
| **NOAA ground-truth validation** (new) | `streaming/ground_truth_validator.py`, `db` table `weather_ground_truth_validation` | Free, unauthenticated NCEI Access Data Service, Buffalo Niagara Airport station `USW00014733` (verified live). Runs on a timer (not Kafka-driven, since NOAA publishes daily), compares the agent's own daily adverse-frame rate against NOAA's reported precipitation/snow for the same day, and writes an agreement verdict per day. |
| **Dedicated validation dashboard** (new) | `observability/grafana/dashboards/weather_validation.json` | A **separate** Grafana dashboard ("RoadWatch — Weather Context & Ground-Truth Validation"), so the original "RoadWatch — Observability & Reliability" dashboard is untouched. |
| Buffalo live-camera sampler (scaffolded, manual) | `streaming/tools/buffalo_camera_sampler.py` | Not wired into the pipeline; run by hand once a `NY511_API_KEY` is obtained. Documented known risk: 511NY's 2026 platform migration may break the legacy endpoint for some keys — the script detects and reports this rather than failing silently. |
| Dataset backlog with acquisition steps | `data/external_datasets/README.md` | Tracks 1–3 (RDD2022 full, ACDC, Ithaca365, CADC, BDD100K, DAWN, MWD, 5-Class) — all require manual download due to size (multi-GB) and/or account/license gates (Kaggle login, institutional dataset request forms). Documented with exact links and commands rather than attempted automatically. |

## Why Tracks 1–3 are documented, not auto-downloaded

- **Size**: several of these (RDD2022 full, BDD100K, ACDC) are multi-gigabyte releases; pulling them into this environment/repo isn't appropriate for a demo project.
- **Access gates**: ACDC, Ithaca365, CADC require per-request dataset access approval; Kaggle sets (DAWN, MWD, 5-Class) require an authenticated Kaggle account/API token that isn't available in this environment.
- **Reproducibility**: `model/prepare_subset.py` and `model/train.py` already know how to consume RDD2022-format data, so once any of these are pulled locally by a developer with the right credentials, retraining is a drop-in next step — no new pipeline code is required for that part.

## Suggested path (unchanged from the original proposal, now partially executed)

1. ~~Immediate/cheap: prototype context tagging without waiting on anyone~~ → **done** in Phase 1 (heuristics + Open-Meteo).
2. **Validate geographically** → **done for the free piece** (NOAA ground-truth validator); **scaffolded, blocked on a developer key** for the live-camera piece (511NY).
3. Strongest external reference for "why this matters": **Ithaca365** and **CADC** remain the recommended citations for the proposal — both require dataset-access requests, so they stay in the backlog ([`data/external_datasets/README.md`](../data/external_datasets/README.md)) until approved.
