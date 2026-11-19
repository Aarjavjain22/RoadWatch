# RoadWatch Buffalo / Weather Handoff — Read This First

**From:** Ziyad’s workstation extension (Buffalo GoPro stream, weather agent, local UI, Buffalo evals)  
**For:** Aarjav — start your extension **without** re-exporting frames, re-training the weather classifier, or rediscovering repo layout.

## 1. Unpack (one minute)

1. Clone or open the RoadWatch repo (Aarjav’s portfolio repo is fine as the base).
2. Extract **this zip** so paths line up with the repo root:

```text
<repo-root>/
  handoff/                    ← docs (you are here)
  model/weights/              ← merged from zip payload
  data/buffalo_eval_frames/   ← merged from zip payload
  data/weather_cls_buffalo/   ← buffalo_split.json only (in minimal zip)
  results/                    ← merged eval artifacts
  source_overlay/             ← copy onto repo root (see below)
```

3. **Overlay source code** (extension not fully on GitHub `main` yet):

```bash
# From repo root (Git Bash / WSL / macOS / Linux)
cp -r source_overlay/* .
# Windows PowerShell alternative:
# Get-ChildItem source_overlay -Recurse | ForEach-Object { ... }  # or use robocopy
```

On Windows, `robocopy source_overlay . /E` from repo root also works.

4. Copy environment hints:

```bash
cp handoff/.env.handoff.example .env
# Edit paths/ports if needed; merge with any secrets you already have.
```

## 2. Feed the AI agent

Open **`handoff/AARJAV_CODEX_BRIEF.md`** and paste it into Agentic AI Codex (or Cursor) as the **project brief** for this handoff. It contains scope, architecture, do-not-duplicate list, and verification commands.

Use **`handoff/INTEGRATION_CHECKLIST.md`** as a human + agent task list.

## 3. Optional large assets (not in this zip)

| Asset | Size | When you need it |
|--------|------|------------------|
| `data/external_datasets/Road Damage GoPro Videos Buffalo/` | ~11 GB | Live Kafka producer video mode, UI “Refresh videos” on originals, re-running `--export_frames` from video |
| Full `data/weather_cls_buffalo/` train images | ~183 MB | Re-training the weather classifier from scratch |
| `runs/roadwatch/yolov8s_norway_ft/weights/best.pt` | ~22 MB | Norway fine-tuned **detection** checkpoint (ask Ziyad; not Buffalo-trained) |

Without the GoPro folder you can still: run eval scripts on **`data/buffalo_eval_frames/`**, load **`weather_cls_buffalo.pt`**, and build on **`docs/WEATHER_AGENT.md`** + pipeline code in `source_overlay/`.

## 4. Quick verify (5 minutes)

```bash
# Weather weights present
ls -la model/weights/weather_cls_buffalo.pt model/weights/best.pt

# Eval frames
ls data/buffalo_eval_frames | head

# Optional: replay eval on exported frames only (no videos)
python model/eval_weather_buffalo.py --frames_dir data/buffalo_eval_frames \
  --weights model/weights/weather_cls_buffalo.pt \
  --out_json results/verify_handoff_weather.json

# Stack (if Docker available)
docker compose up -d
# UI: python -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

## 5. Questions / updates

If something is missing from the zip, check `handoff/MANIFEST.json` (generated when the zip was built) for the exact file list and SHA placeholders.