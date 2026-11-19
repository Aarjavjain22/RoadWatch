# Agent brief: RoadWatch Buffalo extension handoff (ingest as Codex / Cursor project context)

## Your role

You are helping **Aarjav** extend **RoadWatch**, an observable Kafka streaming platform for road-damage detection (YOLOv8s) with a parallel **weather & context-enrichment agent**. A teammate (**Ziyad**) implemented the **Buffalo GoPro stream**, **weather classifier fine-tune**, **evaluation harnesses**, and **local FastAPI UI**. Your job is to **build Aarjav’s extension on top of this handoff**, not redo Ziyad’s work.

## Non-goals (do NOT duplicate)

1. **Do not** re-export Buffalo frames from video if `data/buffalo_eval_frames/` is present (~947 JPEGs, 5 s spacing).
2. **Do not** re-train the weather classifier unless changing architecture — use `model/weights/weather_cls_buffalo.pt` (Buffalo fine-tuned) or `weather_cls.pt` (baseline).
3. **Do not** re-implement video→Kafka sampling — use `streaming/producer.py` with `PRODUCER_SOURCE=video`.
4. **Do not** train a Buffalo **damage** YOLO — GoPro drives have **no damage labels**. Detection remains `model/weights/best.pt` (RDD2022); optional Norway fine-tune is separate (`best_norway.pt` if provided).
5. **Do not** fork event contracts without reading `docs/EVENT_CONTRACT.md` and `streaming/contracts.py`.

## What the handoff zip contains

### Artifacts (ready to use)

| Path | Purpose |
|------|---------|
| `model/weights/weather_cls.pt` | YOLOv8n-cls baseline weather model |
| `model/weights/weather_cls_buffalo.pt` | + Buffalo GoPro frames; deploy with `WEATHER_CLS_FILE=weather_cls_buffalo.pt` |
| `model/weights/best.pt` | Road-damage detector (may already exist in git) |
| `data/weather_cls_buffalo/buffalo_split.json` | Train/eval split for honest unseen Buffalo eval |
| `data/buffalo_eval_frames/*.jpg` | Pre-sampled eval frames; skips `eval_weather_buffalo.py --export_frames` |
| `results/buffalo_*.json`, `results/buffalo_*.csv` | Weather + detection-by-condition eval outputs |
| `results/unseen_weather_cls*.json/csv` | Unseen-footage classifier metrics |
| `results/buffalo_frame_labels.csv` | Label sheet for manual refinement |

### Source overlay (`source_overlay/` → copy to repo root)

Buffalo/weather/UI code that may be **missing from GitHub main**:

- `app/` — local UI (Live monitor, Detection replay, Weather insights, Dataset & records)
- `docs/WEATHER_AGENT.md`, `docs/DATASET_EXPANSION_PLAN.md`
- `model/eval_weather_buffalo.py`, `model/eval_detection_buffalo.py`, `model/prepare_weather_cls.py`, `model/prepare_weather_cls_buffalo.py`
- `streaming/weather_agent.py`, `streaming/image_heuristics.py`, `streaming/ground_truth_validator.py`
- `tests/test_buffalo_video.py`
- `observability/grafana/dashboards/weather_validation.json`

You may still need to **merge** modified files from Ziyad’s branch: `docker-compose.yml`, `db/init.sql`, `streaming/producer.py`, `streaming/contracts.py`, `.env.example`, `README.md` — diff against your clone and port weather-agent service, topics, and mounts.

## Architecture (must respect)

```text
producer.py (images OR Buffalo video @ VIDEO_SAMPLE_FPS)
    → Kafka raw_frames
        → yolo_consumer.py → road_damage_events → analytics_consumer → Postgres detection_events
        → weather_agent.py → frame_weather_context → Postgres frame_weather_context
Postgres view: detection_weather = detection ⨝ weather (adjusted_confidence)
UI (FastAPI): reads Postgres (live/weather) + SQLite (replay/reports)
```

**Frame IDs (video mode):** `video_<clip>_<offset_ms>` (e.g. `video_6_night_00412000`).  
**Stream previews:** last ~300 JPEGs in `data/stream_frames/` for Live monitor.  
**Open-Meteo** uses Buffalo lat/lon at **runtime** — not historical weather for old GoPro footage.

## Environment (start here)

Copy `handoff/.env.handoff.example` → `.env`:

```bash
PRODUCER_SOURCE=video          # needs GoPro folder; use images if videos absent
VIDEO_SAMPLE_FPS=1
WEATHER_CLS_FILE=weather_cls_buffalo.pt
MODEL_WEIGHTS_FILE=best.pt
WEATHER_LAT=42.8864
WEATHER_LON=-78.8784
POSTGRES_HOST_PORT=5433        # docker-compose default in extension
```

## First-session plan for the coding agent

### Phase A — Integrate handoff (30 min)

1. Unzip handoff; `cp -r source_overlay/* .` (or robocopy on Windows).
2. Merge `docker-compose.yml` weather-agent service if missing; apply `db/init.sql` weather tables/views.
3. Place weights under `model/weights/`; set `.env` as above.
4. `pip install -r streaming/requirements.txt` (and UI deps if running `app/`).
5. Run tests: `pytest tests/test_buffalo_video.py tests/test_pipeline.py -q`.

### Phase B — Verify without 11 GB videos (15 min)

```bash
python model/eval_weather_buffalo.py \
  --frames_dir data/buffalo_eval_frames \
  --weights model/weights/weather_cls_buffalo.pt \
  --exclude_json data/weather_cls_buffalo/buffalo_split.json \
  --out_json results/codex_verify_unseen.json

python model/eval_detection_buffalo.py \
  --frames_dir data/buffalo_eval_frames \
  --weather_csv results/buffalo_weather_predictions_buffalo_cls.csv \
  --weights model/weights/best.pt
```

Compare new JSON to bundled `results/*.json` — numbers should be close (same seeds/weights).

### Phase C — Aarjav’s extension (your product work)

Pick **one** vertical; extend without breaking contracts:

| Track | Suggested starting points | Handoff leverage |
|-------|---------------------------|------------------|
| **Streaming / reliability** | Consumer lag, DLQ, replay tests | Existing Kafka topology; don’t change `event_id` format |
| **Detection** | Norway fine-tune, new eval splits | `results/norway_*.json`; no Buffalo damage labels |
| **Weather / trust** | New signals in `classify_visibility`, Grafana | `weather_agent.py`, `WEATHER_AGENT.md`, precomputed eval CSVs |
| **UI / ops** | New pages on `app/main.py` | `app/pipeline.py` SQL patterns |
| **Buffalo live data** | 511NY sampler | `streaming/tools/buffalo_camera_sampler.py` (in full repo; key optional) |

Document new env vars in `.env.example` and new events in `EVENT_CONTRACT.md`.

## Key files to read before coding

1. `docs/WEATHER_AGENT.md` — Buffalo stream, eval methodology, classifier caveats  
2. `docs/ARCHITECTURE.md` — Kafka topics, consumer groups  
3. `streaming/contracts.py` — `classify_visibility`, class enums  
4. `app/replay.py` — offline replay (separate from Kafka)  
5. `model/eval_weather_buffalo.py` — frame export, unseen eval  

## Verification checklist

- [ ] `weather_cls_buffalo.pt` loads in weather-agent container mount  
- [ ] `detection_weather` view returns rows after producer + yolo + weather running  
- [ ] UI `/api/live` and `/api/weather-insights` online when Postgres up  
- [ ] `eval_weather_buffalo.py` runs with `--frames_dir` only (no videos)  
- [ ] No duplicate dataset prep if `buffalo_eval_frames/` exists  

## Explicit truth for stakeholders

- **Weather YOLO** was fine-tuned on Buffalo **frames** (classification only).  
- **Damage YOLO** was **not** trained on Buffalo video; Buffalo is for **streaming + behavioral eval** only.

## Agent execution style

- Minimize diff scope; match existing naming and Docker patterns.  
- Prefer extending `contracts.py` and SQL views over ad-hoc joins in the UI.  
- When unsure if a file is in git, check `handoff/MANIFEST.json` from the zip build.

---

*End of brief — pair with `INTEGRATION_CHECKLIST.md` and `README_FIRST.md`.*