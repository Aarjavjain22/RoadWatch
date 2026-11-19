# Integration checklist (Aarjav + AI agent)

## Receive package

- [ ] Download / extract `roadwatch-aarjav-handoff.zip`
- [ ] Read `handoff/README_FIRST.md`
- [ ] Paste `handoff/AARJAV_CODEX_BRIEF.md` into Codex/Cursor as project context

## Install payload

- [ ] Overlay `source_overlay/` → repo root
- [ ] Confirm `model/weights/weather_cls_buffalo.pt` exists
- [ ] Confirm `data/buffalo_eval_frames/` has hundreds of `video_*_*.jpg` files
- [ ] Confirm `data/weather_cls_buffalo/buffalo_split.json` exists
- [ ] Copy `handoff/.env.handoff.example` → `.env` and adjust ports

## Merge platform wiring (if clone is upstream-only)

- [ ] `docker-compose.yml` — `weather-agent`, `ground-truth-validator`, volume mounts for weights + Buffalo videos path
- [ ] `db/init.sql` — `frame_weather_context`, `detection_weather` view, `weather_ground_truth_validation`
- [ ] Kafka topic `frame_weather_context` in compose init
- [ ] `streaming/producer.py` — video mode + `STREAM_FRAMES_DIR`
- [ ] `.env.example` — document `WEATHER_CLS_FILE`, `PRODUCER_SOURCE`, `VIDEO_SAMPLE_FPS`

## Optional assets (coordinate with Ziyad)

- [ ] GoPro videos at `data/external_datasets/Road Damage GoPro Videos Buffalo/` (~11 GB)
- [ ] `best_norway.pt` for Norway detection fine-tune experiments

## Verify

- [ ] `pytest tests/test_buffalo_video.py -q`
- [ ] Weather eval on frames only (no videos):
  ```bash
  python model/eval_weather_buffalo.py --frames_dir data/buffalo_eval_frames \
    --weights model/weights/weather_cls_buffalo.pt \
    --exclude_json data/weather_cls_buffalo/buffalo_split.json
  ```
- [ ] `docker compose up -d` → Grafana/Prometheus healthy
- [ ] `python -m uvicorn app.main:app --port 8080` → Live + Weather pages

## Start extension work

- [ ] Choose track (streaming / detection / weather / UI / live Buffalo)
- [ ] Write short design note in `docs/` before large changes
- [ ] Keep event schema backward compatible unless versioning