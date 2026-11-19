"""Paths and runtime settings for the local RoadWatch UI."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Share docker-compose's .env so the UI targets the same Postgres; real env vars win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")

WEIGHTS_DIR = ROOT / "model" / "weights"
# Weather classifier checkpoint; WEATHER_CLS_FILE in .env switches it (also read by docker-compose).
WEATHER_CLS_WEIGHTS = WEIGHTS_DIR / os.getenv("WEATHER_CLS_FILE", "weather_cls.pt")
VIDEOS_DIR = ROOT / "data" / "videos"
UI_DIR = ROOT / "data" / "ui"
EVIDENCE_DIR = UI_DIR / "evidence"
REPORTS_DIR = UI_DIR / "reports"
FRAMES_DIR = ROOT / "data" / "frames"
STREAM_FRAMES_DIR = ROOT / "data" / "stream_frames"
EXTERNAL_DATASETS_DIR = ROOT / "data" / "external_datasets"
# GoPro Buffalo footage — registered in Detection replay via Refresh (no copy; ~11 GB stays here).
BUFFALO_GOPRO_VIDEOS_DIR = EXTERNAL_DATASETS_DIR / "Road Damage GoPro Videos Buffalo"
EXTRA_VIDEO_LIBRARY_DIRS: tuple[Path, ...] = (
    BUFFALO_GOPRO_VIDEOS_DIR,
)
RESULTS_DIR = ROOT / "results"
DB_PATH = UI_DIR / "roadwatch_ui.db"

# "auto" picks CUDA when available; set RW_DEVICE=cpu while the GPU is busy training.
DEVICE = os.getenv("RW_DEVICE", "auto")
SAMPLE_FPS = float(os.getenv("RW_SAMPLE_FPS", "5"))
CONF_THRESHOLD = float(os.getenv("RW_CONF_THRESHOLD", "0.25"))
MAX_EVIDENCE_PER_HAZARD = 10

PG_DSN = dict(
    host=os.getenv("POSTGRES_HOST", "localhost"),
    port=int(os.getenv("POSTGRES_HOST_PORT", "5432")),
    dbname=os.getenv("POSTGRES_DB", "roadwatch"),
    user=os.getenv("POSTGRES_USER", "roadwatch"),
    password=os.getenv("POSTGRES_PASSWORD", "roadwatch"),
    connect_timeout=2,
)

for d in (VIDEOS_DIR, EVIDENCE_DIR, REPORTS_DIR):
    d.mkdir(parents=True, exist_ok=True)
