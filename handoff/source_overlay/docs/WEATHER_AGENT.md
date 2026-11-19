# Weather & Context-Enrichment Agent

Phase 1 implementation of the agent proposed in `RoadWatch_Weather_Agent.pptx`: making the pipeline aware of **when** it can be trusted, not just **what** it detects.

## Problem

RoadWatch's detector is trained on RDD2022, mostly clear-weather, daytime imagery. Buffalo deployment brings snow, ice glare, rain, fog and low light — conditions the model has barely seen. Today, no step in the pipeline records whether a detection came from a trustworthy frame; a low-confidence call in a snowstorm looks identical to one on a clear day.

## What this agent does (Phase 1 — implemented)

`streaming/weather_agent.py` is a new, independent Kafka consumer — the same architectural pattern as `yolo_consumer.py` and `analytics_consumer.py` — so it drops into the existing pipeline with **no changes to the YOLO detector**.

1. **Context tagging** — for every frame on `raw_frames`:
   - Image heuristics (`compute_image_heuristics`): brightness, contrast, blur (Laplacian variance), glare ratio.
   - External weather lookup (`fetch_weather`): current temperature, precipitation, cloud cover, visibility and WMO weather code from [Open-Meteo](https://open-meteo.com/) (free, no API key), for the camera's configured location (`WEATHER_LAT` / `WEATHER_LON`, defaults to Buffalo, NY). Results are cached per 15-minute bucket to stay API-friendly, and any API failure degrades to `"unknown"` weather rather than blocking the pipeline.
   - Image weather classifier (`classify_weather_image`, optional): a YOLOv8n-cls model (`model/weights/weather_cls.pt`) that labels the frame `clear` / `cloudy` / `fog` / `rain` / `snow`. It is the only signal that looks at the *scene* rather than pixel statistics or a city-wide API reading. See [Weather classifier](#weather-classifier-step-2) below.
2. **Verdict** (`streaming/contracts.classify_visibility`, pure/unit-tested):
   - `visibility_condition` — `"clear"` or a `+`-joined list of degrading factors (e.g. `"low_light+snow"`).
   - `confidence_adjustment` — a multiplicative factor in `(0, 1]` downstream consumers can apply to detection confidence.
   - `escalation_flag` — `true` when a frame is degraded enough to deserve a second look.
3. **Emit + persist** — publishes a `frame_weather_context` event (see [Event Contracts](EVENT_CONTRACT.md)) and writes it idempotently to the `frame_weather_context` PostgreSQL table, surfaced on the Grafana dashboard (visibility-condition breakdown, escalation count, weather API health, tagged/failed frames).

## Weather classifier (Step 2)

Trained locally on the GTX 1650 from the three downloaded weather datasets, merged into one ImageFolder layout by `model/prepare_weather_cls.py`:

```bash
python model/prepare_weather_cls.py            # -> data/weather_cls/{train,val}/{clear,cloudy,fog,rain,snow}
python model/train.py --data data/weather_cls --model yolov8n-cls.pt --imgsz 224 \
  --epochs 30 --batch 32 --device 0 --workers 2 --patience 8 --name weather_cls
cp runs/roadwatch/weather_cls/weights/best.pt model/weights/weather_cls.pt
```

| Metric | Value |
|---|---|
| Train / val images | 3,181 / 794 |
| Top-1 accuracy (val) | **97.2%** (best epoch 20, early-stopped at 28) |
| Training time | ~6 minutes |

**Caveat:** `fog` and `snow` have only ~50 images each, so the headline accuracy is dominated by `clear` / `cloudy` / `rain`. That is why the verdict is gated: only an adverse class (`fog`, `rain`, `snow`) at confidence ≥ 0.6 (`ML_CONFIDENCE_MIN`) adds an `ml_<class>` reason and a ×0.80 adjustment. A weak or benign prediction never escalates a frame on its own.

The `weather-agent` container mounts `./model/weights` read-only and loads the classifier at startup if the file exists; without it the agent falls back to heuristics + Open-Meteo, exactly as in Phase 1.

## Detection × weather join (Step 3)

`db/init.sql` defines a `detection_weather` view that left-joins every `detection_events` row with its frame's `frame_weather_context` row and exposes `adjusted_confidence = confidence × confidence_adjustment`. It powers the **Weather insights** page of the local UI and can be queried directly:

```bash
docker exec roadwatch-postgres psql -U roadwatch -d roadwatch -c \
  "SELECT visibility_condition, COUNT(*), AVG(confidence), AVG(adjusted_confidence) FROM detection_weather GROUP BY 1 ORDER BY 2 DESC;"
```

## Ground-truth validation (new)

`streaming/ground_truth_validator.py` closes the "does this actually match reality in Buffalo?" loop from the dataset-diversity plan. It is **not** a Kafka consumer — NOAA's daily summaries only change once a day, so it runs on a simple timer (`VALIDATION_INTERVAL_SEC`, default 120s):

1. Fetches the most recent available daily summary for Buffalo Niagara International Airport (`USW00014733`) from NOAA's free, unauthenticated [NCEI Access Data Service](https://www.ncei.noaa.gov/support/access-data-service-api-user-documentation).
2. Reads the Weather Agent's own output back out of `frame_weather_context` for that calendar day and computes the fraction of frames tagged non-`"clear"`.
3. Compares the two (`streaming/contracts.classify_noaa_conditions`, `ground_truth_agreement`) and upserts one row per day into `weather_ground_truth_validation`.

**Known caveat:** NOAA GHCN daily summaries lag real time by a few days while QA checks complete. Since this is a replay pipeline, "today's" agent frames are always compared against NOAA's *most recently published* reading (not necessarily literally today) — the `is_exact_date_match` column and `noaa_observed_date` field make that gap visible instead of hiding it.

This is surfaced on its **own** Grafana dashboard — **"RoadWatch — Weather Context & Ground-Truth Validation"** — rather than added to the original observability dashboard. See [`docs/DATASET_EXPANSION_PLAN.md`](DATASET_EXPANSION_PLAN.md) for the full dataset research this grew out of, including the (currently backlogged) live-camera and training-data tracks.

## Buffalo GoPro drives: live stream + evaluation

**Stream.** With `PRODUCER_SOURCE=video` (the default in `.env`), `producer.py` samples `VIDEO_SAMPLE_FPS` (default 1) frames per second from the eight Buffalo GoPro clips and paces them in real time, so the pipeline and the Live monitor run on real Buffalo roads instead of the mixed-country RDD2022 images. Frame ids encode clip and position (`video_6_night_00412000` = Video 6, 412 s), so replay passes upsert the same rows. The newest 300 frames are written to `data/stream_frames/` for the UI; nothing else is copied to disk. At 1 fps the CPU containers keep up on an 8-core laptop as long as nothing else heavy runs on the CPU.

**No historical weather.** The clips were re-exported with Clipchamp, which stripped the GoPro GPS track and recording timestamps, so the Open-Meteo reading describes Buffalo *now*, not the drive. The evaluation therefore scores only the image-based signals.

**Evaluation** (`model/eval_weather_buffalo.py`, one frame every 5 s, 947 frames; labels from clip names — `Night` → night, `Rainy` → rain):

| Task | Prediction | Precision | Recall | Accuracy |
|---|---|---|---|---|
| Night (306 frames) | `low_light` reason | 1.000 | 1.000 | 1.000 |
| Rain (63 frames) | `ml_rain` reason | 0.062 | 0.016 | 0.919 |
| Degraded (night or rain) | condition ≠ `clear` | 1.000 | 0.832 | 0.935 |

Results: `results/buffalo_weather_eval.json`; per-frame predictions: `results/buffalo_weather_predictions.csv`.

Findings:

- **Letterbox bars caused false low-light flags.** Video 2 ("Cropped") has a black bar over the top 57% of the frame, which pulled mean brightness to 56 and flagged 78% of its daytime frames as `low_light`. `image_heuristics.crop_letterbox` now removes such bars before heuristics and the classifier (agent, UI replay and evaluation). It only accepts a bar that ends in a sharp edge into bright content, because night sky is just as dark but fades gradually; on these clips it trims all 63 cropped frames and none of the 306 night frames. Before/after: night precision 0.862 → 1.000 (`results/buffalo_weather_eval_no_crop.json`).
- **The original classifier did not recognise Buffalo rain** (`weather_cls.pt`). Video 2 shows a wet road and droplets under a teal-graded sky; it called 97% of it `clear`. Its rain training images are overcast grey scenes, so it keys on overall colour; no inference-time trick (white balance, desaturation, tiling, squashing) fixed this. It also produced spurious `rain`/`fog` on 20 of 306 night frames, because 98.5% of its training images are daytime.
- **Fix 1 — ignore the classifier in low light** (`LOW_LIGHT_BRIGHTNESS = 60` in `contracts.py`). The brightness rule alone already gets night right (precision/recall 1.0), so the classifier adds nothing there.
- **Fix 2 — fine-tune on Buffalo frames** (`weather_cls_buffalo.pt`, `model/prepare_weather_cls_buffalo.py`). Training adds 188 rainy frames (Video 2, first 188 s) and 400 dry frames (Videos 4, 5), all cropped to the same horizon-and-road band so frame shape cannot signal rain. Videos 1, 3, 8 and the last part of Video 2 are never trained on; `buffalo_split.json` records the split and `eval_weather_buffalo.py --exclude_json` scores only unseen footage (with a 30 s guard gap). Original validation top-1 stays 96.7% (was 97.2%).

  | Unseen footage only (586 frames) | original | Buffalo fine-tune |
  |---|---|---|
  | rain recall (19 unseen rainy frames) | 0.158 | **0.947** |
  | rain precision | 1.000 | 0.857 |
  | false rain flags on held-out dry clips | 0 / 261 | 3 / 261 |
  | same, dry frames cropped to the rainy strip's shape | 3 / 261 | 10 / 261 |
  | night precision / recall | 1.0 / 1.0 | 1.0 / 1.0 |

  Results: `results/unseen_weather_cls*.json`. **Caveat:** there is only one rainy drive, so the rain test is a time-split of that same drive (same weather, camera, route type), not a different rainstorm; 19 frames is a small sample. The score for a new rain event is likely lower. More rainy Buffalo footage is needed to validate it properly. The full-clip numbers in `results/buffalo_weather_eval_buffalo_cls.json` include training frames and must not be quoted as accuracy.
- **Deployment:** `WEATHER_CLS_FILE=weather_cls_buffalo.pt` in `.env` (used by docker-compose and the UI). Rollback: set `weather_cls.pt` and `docker compose up -d weather-agent`, restart the UI. The original weights file is untouched.
- **Letterbox rule (update):** a bar is now ≥5% of pure-black rows (night sky is never that flat), plus the two blended rows after it; the earlier "sharp edge into bright content" test failed on frames under a bridge.
- **Clip-name labels are coarse.** Every frame inherits its clip's label. For frame-level ground truth, review `results/buffalo_frame_labels.csv` (written by `--export_frames`, labels pre-filled from clip names, frames in `data/buffalo_eval_frames/`) and re-run with `--labels results/buffalo_frame_labels.csv`.

```bash
python model/eval_weather_buffalo.py --device 0                       # clip-name labels
python model/eval_weather_buffalo.py --device 0 --no_letterbox_crop   # previous behaviour
python model/eval_weather_buffalo.py --device 0 --labels results/buffalo_frame_labels.csv \
    --out_json results/buffalo_weather_eval_labeled.json
```

### Detector output by condition (no ground truth)

`python model/eval_detection_buffalo.py --strip_band 0.34 0.73 --weather_csv results/buffalo_weather_predictions_buffalo_cls.csv` runs `best.pt` at conf 0.25 on the exported frames (`results/buffalo_detection_by_condition.json`, sample sheets in `results/buffalo_detection_samples/`). The drives have no damage labels, so this shows behaviour, not accuracy.

| condition | frames | with detection | detections /100 frames | conf ≥ 0.5 /100 | median conf | mean weather adjustment |
|---|---|---|---|---|---|---|
| dry day (full frame) | 578 | 27.7% | 34.8 | 10.7 | 0.404 | 0.999 |
| dry day cropped to rainy-strip shape | 578 | 4.5% | 5.0 | 0.3 | 0.334 | – |
| rain (Video 2 strip) | 63 | 0.0% | 0 | 0 | – | – |
| night | 306 | 18.0% | 22.9 | 5.2 | 0.398 | 0.600 |

The rainy clip's zero detections should not be read as "rain hides damage": the same dry footage cropped to that strip also drops from 34.8 to 5.0 per 100 frames, because the strip excludes most of the road surface near the car. Night detections viewed in the sample sheet are mostly real-looking longitudinal cracks, with lower confidence; the agent's night adjustment (×0.6 on average) discounts them. Real detection recall per condition needs roughly 150–200 hand-annotated frames per condition.

## What this agent does not do yet

- **Phase 2 (stretch):** route ambiguous, high-severity, low-visibility detections to a VLM reasoning step ("is this really a crack, or a shadow / wet-road reflection?"), and produce an automated weekly performance-by-condition report.
- **Phase 3 (future work):** feed findings into targeted retraining/augmentation, and per-camera geolocation once multi-camera feeds (NITTEC / Lincoln / bus) are available.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `WEATHER_LAT` / `WEATHER_LON` | `42.8864` / `-78.8784` (Buffalo, NY) | Location used for the external weather lookup |
| `WEATHER_API_URL` | `https://api.open-meteo.com/v1/forecast` | External weather API endpoint |
| `WEATHER_CACHE_TTL_SEC` | `900` | Weather lookup cache bucket size, in seconds |
| `WEATHER_TIMEOUT_SEC` | `3.0` | HTTP timeout for the weather API call |
| `WEATHER_CLS_WEIGHTS` | `/app/weights/weather_cls.pt` (compose) | Image weather classifier; skipped if the file is missing |
| `POSTGRES_DSN`, `KAFKA_BOOTSTRAP`, `METRICS_PORT` | shared pipeline conventions | Same as other consumers (default metrics port `8004`) |
| `NOAA_STATION_ID` | `USW00014733` (Buffalo Niagara Int'l Airport) | GHCN station used by `ground_truth_validator.py` |
| `PRODUCER_SOURCE` | `images` (compose) / `video` (`.env`) | Producer input: `data/frames` images or the Buffalo GoPro clips |
| `VIDEO_SAMPLE_FPS` | `1` | Frames per second of footage streamed in video mode (paced in real time) |
| `VALIDATION_INTERVAL_SEC` | `120` | How often the ground-truth validator re-checks (default metrics port `8005`) |

## Running it

Already wired into `docker-compose.yml` as `weather-agent`, starting alongside the rest of the stack:

```bash
docker compose up -d
```

Query enriched context directly:

```bash
docker exec -it roadwatch-postgres \
  psql -U roadwatch -d roadwatch \
  -c "SELECT visibility_condition, COUNT(*), AVG(confidence_adjustment) FROM frame_weather_context GROUP BY 1 ORDER BY 2 DESC;"
```

Metrics: `http://localhost:8004/metrics` (or the "Weather & Context Agent" panels on the Grafana dashboard).
