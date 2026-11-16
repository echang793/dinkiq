"""Session storage layout: paths, ids, and which artifacts are derived.

Each stage writes a durable artifact under data/sessions/<id>/ so any stage is
resumable:
  raw.<ext>          original upload (swept after DINKIQ_KEEP_RAW_DAYS)
  video.mp4          normalized 720p/30fps
  audio.wav          extracted audio (for hit detection)
  frame0.jpg         first frame (calibration UI)
  calibration.json   court corners + subject click (written by the API)
  tracks.parquet     all person tracks (pixel space; survives recalibration)
  ball.parquet       ball candidates/track (pixel space)
  positions.parquet  subject court positions
  metrics.json events.json shots.json points.json dupr.json   derived results
  status.json        {stage, state, error?}   (see session_status)

Orchestration lives elsewhere: session_status, media (ffmpeg/ingest), jobs
(queue + recovery), stages (analysis), notify.
"""

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# DINKIQ_DATA_DIR relocates all mutable state (sessions, uploads, trash, saved
# calibration) -- the test suite points it at a temp dir so it can never touch
# real sessions. Read once at import: server.py derives its paths from SESSIONS.
DATA = Path(os.environ.get("DINKIQ_DATA_DIR") or ROOT / "data")


SESSIONS = DATA / "sessions"


MODELS = ROOT / "models"


FPS = 30.0


# Plain names only: no leading dot (".trash" is the trash folder, not a session),
# no separators, no dots at all. Server-made ids are 12 hex chars; the extra
# room is for hand-made ids like "kitchen_test".
SESSION_ID_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,63}")


def session_dir(sid: str) -> Path:
    if not SESSION_ID_RE.fullmatch(sid or ""):
        raise ValueError("bad session id")
    d = (SESSIONS / sid).resolve()
    if d.parent != SESSIONS.resolve():  # path traversal guard (symlinks)
        raise ValueError("bad session id")
    return d


# artifacts derived from calibration/analysis; tracks.parquet and ball.parquet
# are pixel-space (calibration-independent) and deliberately survive so
# recalibration takes seconds instead of re-running detection
DERIVED = ["positions.parquet", "metrics.json", "events.json", "shots.json",
           "points.json", "dupr.json"]


def clear_derived(sdir: Path) -> None:
    for name in DERIVED:
        (sdir / name).unlink(missing_ok=True)
    (sdir / "highlights.mp4").unlink(missing_ok=True)  # stale once rally clips regenerate
    clips = sdir / "clips"
    if clips.exists():
        for f in clips.iterdir():
            f.unlink()
