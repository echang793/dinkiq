# PickleCoach

Upload pickleball match film → computer-vision analysis → coaching feedback and a
DUPR-style skill estimate. "A pickleball coach that fits in your pocket."

## Setup

```bash
brew install ffmpeg                       # video processing
/opt/homebrew/bin/python3.13 -m venv .venv   # torch requires <=3.13
.venv/bin/pip install -r requirements.txt
```

YOLO weights auto-download to `models/` on first run (yolov8n-pose).

## Run

```bash
.venv/bin/python src/server.py    # http://127.0.0.1:8100
```

`static/dashboard.html` is the reference UI: upload a clip, click 4 court corners +
4 kitchen corners + yourself, watch the progress bar, read the results.

## Pipeline (per session, artifacts under `data/sessions/<id>/`)

| Stage | Module | Artifact | Notes |
|---|---|---|---|
| ingest | `pipeline.py` | `video.mp4`, `audio.wav`, `frame0.jpg`, `ingest.json` | remux fast-path when input is already ≤720p H.264 ~30fps |
| tracking | `tracking.py` | `tracks.parquet` | YOLOv8n-pose + ByteTrack, `vid_stride=2`; ball candidates (`ball.py::BallDetector`) and camera cuts (`CutDetector`) ride the same decode |
| metrics | `metrics.py` | `positions.parquet`, `metrics.json` | subject stitched across track-id breaks; homography anchored by kitchen corners |
| events | `events.py` | `events.json`, `clips/` | audio hits (band-passed 1–6 kHz + transient gate) → rallies; wrist-speed swings attribute shots |
| shots | `ball.py`, `shots.py` | `ball.parquet`, `shots.json` | bounce-projected serve depth; degrades below 15% ball coverage |
| rating | `dupr.py`, `feedback.py` | `dupr.json` | weighted rubric → band + confidence + tips |

Every stage is resumable; `tracks.parquet`/`ball.parquet` survive recalibration so
it takes seconds.

## Rubric calibration

1. Upload + calibrate clips of players with known DUPR ratings.
2. Set "Known DUPR" in Session info for each.
3. `python3 src/calibrate_rubric.py` (needs ≥4 tagged sessions) — fits per-dimension
   anchor shifts, writes `models/rubric_calibrated.json`, reports before/after MAE.
   Re-analyze old sessions to apply.

## Tests

```bash
.venv/bin/python -m pytest tests/     # or run any tests/test_*.py standalone
```

## API

See [docs/API.md](docs/API.md) — the contract the UI builds against.
