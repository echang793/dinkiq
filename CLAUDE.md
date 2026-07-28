# dinkiq

## Purpose

DUPR skill estimation and analysis system: video analysis pipeline, rubric calibration, shot classification, and confidence scoring.

## Stack

Python 3.13, FastAPI, Pydantic v2, PyTorch, ffmpeg, SQLite, Parquet.

## Deployment (remote / phone access)

Runs under launchd and is reached through the shared "vantage hub", not
directly. Config lives outside this repo (same convention as the sibling
vantage projects — nothing here is checked in), so it is listed here
because its absence once made a total remote-access outage invisible:

- `~/Library/LaunchAgents/com.vantage.dinkiq.plist` — RunAtLoad + KeepAlive
- `~/.vantage/scripts/dinkiq-start.sh` — launcher (uses this repo's `.venv`)
- `~/.vantage/Caddyfile` — reverse-proxies :8100 at **`/app/dinkiq/`**
  (generated from `~/.vantage/projects.json`; do not hand-edit)
- Public URL: `https://<tailnet-host>/app/dinkiq/`, gated by Caddy basic_auth

Caddy **strips** the `/app/dinkiq` prefix and sends `X-Forwarded-Prefix`.
`server._spa()` echoes it back as `window.__BASE__` and the frontend
prefixes every root-absolute URL with it — without that, the page loads
but every `/api/...` call resolves against the hub origin, 404s, and the
app falls silently into sample-data demo mode. Keep new frontend URLs
`BASE`-prefixed. Served directly (localhost/LAN) `BASE` is `""`.

Logs: `~/.vantage/logs/dinkiq.log`.

## Entry points

- `src/server.py` — FastAPI server at http://127.0.0.1:8100
- `src/calibrate_rubric.py` — Fit rubric vs known DUPR sessions
- `src/analyze` — Analysis pipeline (video → tracks → shots → skill)

## Commands

```bash
.venv/bin/python src/server.py          # Serve at http://127.0.0.1:8100
.venv/bin/python -m pytest tests/       # Run all test suites
python3 src/calibrate_rubric.py         # Fit rubric vs known DUPR sessions
```

Venv is Python 3.13 (torch incompatible with system 3.14); ffmpeg via brew.

## Invariants

- **`dupr.RUBRIC`** is the single tuning point; `models/rubric_calibrated.json` overrides anchors only
- **One decode pass**: ball detection and camera cuts fed from tracking loop; never add a second decode
- **`tracks.parquet` / `ball.parquet` are pixel-space** and survive recalibration; court-derived data is disposable
- **Frame numbers in parquets are REAL video frames** (tracking stride=2); time = frame / FPS
- **Homography**: outer corners user-estimated, kitchen corners anchor the fit; ball valid only at bounces
- **Honest degradation**: gate shots on ball coverage; apply confidence penalties (short sample, geometry, camera cuts)
- **Single analysis worker** (`pipeline.enqueue_analyze`) — no parallel analyze threads (MPS contention)

## Gotchas

- PostToolUse hook runs `ruff check --fix` and DELETES unused imports — add imports in same edit as code
- `py_compile` passes with missing imports — verify by importing the module
- Court lines are any color (not necessarily white)
- UI copy: DUPR estimates are "resembles ~X.X", never certified ratings

