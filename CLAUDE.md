# CLAUDE.md

Guidance for Claude Code in this repository.

## Commands

```bash
.venv/bin/python src/server.py           # app at http://127.0.0.1:8100
.venv/bin/python -m pytest tests/       # all suites (each also runs standalone)
python3 src/calibrate_rubric.py          # fit rubric vs known-DUPR sessions
```

Venv is Python 3.13 (torch does not support the system 3.14). ffmpeg via brew.

## Architecture

Stage pipeline per session under `data/sessions/<id>/`; each stage writes a durable
artifact and is resumable — see README table. FastAPI server (`src/server.py`) +
reference SPA (`static/dashboard.html`); production UI arrives from Claude Design
as zip drops and builds against `docs/API.md`.

## Invariants (do not break)

- **`dupr.RUBRIC` is the single tuning point** for skill estimation — dimensions,
  weights, anchors. `models/rubric_calibrated.json` (written by
  `calibrate_rubric.py`) overrides anchors only, via `dupr.active_anchors`.
- **One decode pass**: ball candidates (`ball.BallDetector`) and camera cuts
  (`ball.CutDetector`) are fed from the tracking loop's `frame_cb`. Never add a
  second full video decode to `analyze`.
- **`tracks.parquet` / `ball.parquet` are pixel-space** and survive recalibration
  (`pipeline.clear_derived` keeps them). Anything court-coordinate-derived is
  disposable.
- **Frame numbers in parquets are REAL video frames** (tracking runs at
  `vid_stride=2`, so rows step by 2). Time = frame / FPS (30). Don't assume
  consecutive rows are 1 frame apart.
- **Homography**: 4 outer corners may be off-frame user estimates; 4 kitchen
  corners anchor the fit near the net (`court.CourtCalibration`). Ball positions
  are court-valid ONLY at bounces.
- **Honest degradation**: shots gate on ball coverage; DUPR confidence has a
  penalty stack (short sample, off-frame corners, no kitchen calibration, camera
  cuts, geometry warnings). Never report ball-derived metrics without the gate.
- **Single analysis worker** (`pipeline.enqueue_analyze`) — MPS contention; don't
  spawn parallel analyze threads.

## Gotchas

- A global PostToolUse hook runs `ruff check --fix` after every .py edit and
  DELETES imports that look unused. Add imports in the SAME edit as the code that
  uses them, and re-check imports after any multi-edit message.
- `py_compile` passes with missing imports — verify by importing the module.
- Court lines are any color (not necessarily white); nothing may assume white.
- Email/UI copy: DUPR estimates are "resembles ~X.X", never certified ratings.

## Testing

Run the affected `tests/test_*.py` after every change; run all suites before
declaring done. Synthetic fixtures (drawn balls, click audio, fake sessions) are
the pattern — no large binary fixtures in the repo.
