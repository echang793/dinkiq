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
- `~/.vantage/Caddyfile` — reverse-proxies :8100 at the funnel **root**
  (generated from `~/.vantage/projects.json`; do not hand-edit)
- Public URL: `https://<tailnet-host>/`, gated by Caddy basic_auth (user
  `eric`, same password as `DINKIQ_PASSWORD`)

The funnel is dedicated to this project: `projects.json` sets
`"root_project": "dinkiq"`, which makes the `:8081` catch-all proxy here
instead of the hub, and every sibling project is `enabled=false`. To
un-dedicate it, drop `root_project`, re-enable the others, regenerate,
`launchctl kickstart -k gui/501/com.vantage.caddy`, and re-add the funnel
ports the root-mounted projects need (`tailscale funnel --https=<p> <p>`).

At the root there is no prefix to strip, so Caddy sends no
`X-Forwarded-Prefix` and `window.__BASE__` is unset — the frontend's
`BASE` falls back to `""`, same as localhost. **The prefix machinery is
still live and still required**: `server._spa()` injects `__BASE__` from
the header whenever one arrives, so if this app is ever moved back under
`/app/dinkiq/` it keeps working. Keep new frontend URLs `BASE`-prefixed;
without that a subpath mount loads the page but every `/api/...` call
404s against the hub origin and the app falls silently into demo mode.

Logs: `~/.vantage/logs/dinkiq.log`.

**launchd gives a minimal PATH without Homebrew.** The wrapper re-exports
`/opt/homebrew/bin` because the pipeline shells out to ffmpeg/ffprobe —
without it the server serves fine but every analysis dies with
`[Errno 2] ... 'ffprobe'`, i.e. silently broken.

Config lives in the repo's gitignored `.env`, loaded process-wide by
`server._load_dotenv()` (so `pipeline` sees it too):
`DINKIQ_PASSWORD`, `DINKIQ_PUBLIC_URL` (deep link in notifications),
`DINKIQ_WEBHOOK_URL` (analysis-finished push; URLs containing `ntfy`
get a plain-text body, anything else Discord/Slack JSON).

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

