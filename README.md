# DinkIQ

Upload pickleball match film → computer-vision analysis → coaching feedback and a
DUPR-style skill estimate. "A pickleball coach that fits in your pocket."

## Setup

```bash
brew install ffmpeg                          # video processing
/opt/homebrew/bin/python3.13 -m venv .venv   # torch requires <=3.13 (see .python-version)
.venv/bin/pip install -r requirements.lock   # the exact tested environment
# development (tests, lint):  .venv/bin/pip install -r requirements-dev.txt
```

`requirements.txt` lists the direct dependencies (pinned); `requirements.lock` is the
full freeze, including transitive ones like torch, and is what the service runs.

Fetch the pose weights **explicitly, into a path with no apostrophe** (ultralytics
silently strips `'` from weight paths, so a repo under e.g. `Eric's MacBook/` never
finds them and tries to download elsewhere):

```bash
.venv/bin/python -c "from ultralytics.utils.downloads import attempt_download_asset as d; d('models/yolov8s-pose.pt')"
```

`GET /api/health` reports a missing tool, missing weights, a low disk or a dead
analysis worker (200 only when everything is fine).

## Run

```bash
.venv/bin/python src/server.py    # http://127.0.0.1:8100
```

`static/dashboard.html` is the reference UI: upload a clip, click 4 court corners +
4 kitchen corners + yourself, watch the progress bar, read the results.

## Share with friends

No real cloud "free tier" can run the torch + YOLO + ffmpeg pipeline (too little
RAM, or GPU cost per hour). Free option: expose your own Mac via
[Tailscale Funnel](https://tailscale.com/kb/1223/funnel). The Funnel fronts a Caddy
reverse proxy (HTTP Basic auth), which proxies to this server on 127.0.0.1:8100 — see
**Deployment** in `CLAUDE.md` for the launchd services, watchdog and backups. Quick local
version:

```bash
export DINKIQ_PASSWORD="something-only-you-and-friends-know"
.venv/bin/python src/server.py     # keep running in one terminal
tailscale funnel 8100              # in another terminal — prints the public https:// URL
```

(Pointing the Funnel straight at :8100 also works — requests then use the app's own
password prompt. The production setup goes through Caddy.)

- Your Mac has to stay awake and the server running for friends to reach it.
- `DINKIQ_PASSWORD` gates everyone except genuinely-local requests (loopback peer,
  loopback Host, no forwarding headers) with an HTTP Basic Auth prompt (any
  username, that password) — leave it unset for local-only use. The server
  listens on 127.0.0.1 only (`DINKIQ_HOST` to change it); a reverse proxy
  proves itself with `DINKIQ_PROXY_SECRET` via the `X-Dinkiq-Proxy` header.
  The Host header is never trusted on its own.
- `tailscale funnel status` shows the current public URL; `tailscale funnel off`
  stops sharing. **If the machine is renamed the Funnel hostname changes** and the
  old config silently serves nothing — re-run `tailscale funnel --bg <caddy-port>`.
- `DINKIQ_WEBHOOK_URL` (optional): an [ntfy](https://ntfy.sh) topic URL (real phone push,
  no account — pick a long random topic name, it is the only secret) or a Discord/Slack
  webhook. Pings when a session finishes analyzing or fails, and when the watchdog sees
  DinkIQ go down or recover.

## Pipeline (per session, artifacts under `data/sessions/<id>/`)

| Stage | Module | Artifact | Notes |
|---|---|---|---|
| ingest | `media.py` | `video.mp4`, `audio.wav`, `frame0.jpg`, `ingest.json` | remux fast-path when input is already ≤720p H.264 ~30fps; original `raw.*` kept `DINKIQ_KEEP_RAW_DAYS` (14) |
| tracking | `tracking.py` | `tracks.parquet`, `cuts.json` | YOLOv8s-pose + ByteTrack, `vid_stride=2`; ball candidates (`ball.py::BallDetector`) and camera cuts (`CutDetector`) ride the same decode |
| metrics | `metrics.py` | `positions.parquet`, `metrics.json` | subject stitched across track-id breaks; homography fit from the clicked corners |
| events | `events.py`, `stages.py` | `events.json`, `clips/` | audio hits (band-passed 1–6 kHz + transient gate) → rallies; wrist-speed swings attribute shots |
| shots | `ball.py`, `shots.py` | `ball.parquet`, `shots.json` | bounce-projected serve depth; degrades below 15% ball coverage |
| points | `points.py` | `points.json` | rally winners + serve/return/shot-type win rates (correctable per rally) |
| rating | `dupr.py`, `feedback.py` | `dupr.json` | weighted rubric → band + confidence + tips + drills |

`stages.py` orchestrates events → rating (`analyze()`), `jobs.py` runs it on the single
analysis worker, `notify.py` pushes the result. Highlights/report/card are built on demand.

Every stage is resumable; `tracks.parquet`/`ball.parquet` survive recalibration so
it takes seconds.

## Rubric calibration

1. Upload + calibrate clips of players with known DUPR ratings.
2. Set "Known DUPR" in Session info for each.
3. `.venv/bin/python src/calibrate_rubric.py` (needs ≥4 tagged sessions) — fits per-dimension
   anchor shifts, writes `models/rubric_calibrated.json`, reports before/after MAE.
   Re-analyze old sessions to apply.

## Tests

```bash
.venv/bin/python -m pytest tests/     # tests use a throwaway data dir (DINKIQ_DATA_DIR); run via pytest, not standalone
```

## API

See [docs/API.md](docs/API.md) — the contract the UI builds against.
