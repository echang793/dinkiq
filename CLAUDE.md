# dinkiq

## Purpose

DUPR skill estimation and analysis system: video analysis pipeline, rubric calibration, shot classification, and confidence scoring.

## Stack

Python 3.13, FastAPI, Pydantic v2, PyTorch (ultralytics YOLOv8-pose), ffmpeg, Parquet. State is plain files
under `data/` (JSON + parquet) — there is no database.

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

**Auth model** (`server._basic_auth`): the app listens on `127.0.0.1` only and
skips its password for exactly two kinds of request — one carrying
`X-Dinkiq-Proxy: $DINKIQ_PROXY_SECRET` (Caddy injects it after its own
basic_auth; the same value lives in `~/.vantage/caddy.env`, generated via the
registry's `proxy_secret_env`), or a genuinely local one (loopback peer, loopback
Host, no `X-Forwarded-*`/`Forwarded`). **Never trust the Host header alone** — it
was the old exemption and any LAN/tailnet client could forge it. Consequence:
`http://<lan-ip>:8100` no longer works; use the funnel URL, or localhost.

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

Logs: the app writes `~/.vantage/logs/dinkiq-app.log` (rotating, 5 MB × 5, timestamped; uvicorn
access + errors too). `dinkiq.log` is launchd's stdout/stderr — startup and crash output only,
capped by the launcher.

**Ops (all outside the repo, in `~/.vantage/scripts/` + `~/Library/LaunchAgents/`):**
- `GET /api/health` — 200 only if ffmpeg/ffprobe are on PATH, disk > 2 GB, data dir writable,
  pose weights present, analysis worker alive. Else 503 with per-check detail.
- `com.vantage.dinkiq.watchdog` (every 5 min) checks app health, the Caddy gate **and the public
  URL**, and pushes to `DINKIQ_WEBHOOK_URL` (ntfy) after 2 failing runs, then again on recovery.
  The public-URL check is what catches a Funnel/hostname problem (the machine was renamed once
  and the Funnel silently kept the old name).
- `com.vantage.dinkiq.backup` (03:15 daily) hard-linked snapshots of `data/` + `.env` into
  `~/Backups/dinkiq/`, 14 days. Same disk — also use Time Machine or an offsite copy.
- The launcher backs off 300 s (instead of spinning) if the repo/venv is missing.
- On startup `jobs.recover_interrupted()` re-queues analyses a restart interrupted and restarts
  interrupted ingests (the queue is in memory only).

**launchd gives a minimal PATH without Homebrew.** The wrapper re-exports
`/opt/homebrew/bin` because the pipeline shells out to ffmpeg/ffprobe —
without it the server serves fine but every analysis dies with
`[Errno 2] ... 'ffprobe'`, i.e. silently broken.

Config lives in the repo's gitignored `.env`, loaded process-wide by
`server._load_dotenv()` (so `pipeline` sees it too):
`DINKIQ_PASSWORD`, `DINKIQ_PROXY_SECRET`, `DINKIQ_PUBLIC_URL` (deep link in notifications),
`DINKIQ_WEBHOOK_URL` (analysis finished/failed + ingest failed push; URLs containing `ntfy`
get a plain-text body, anything else Discord/Slack JSON).
Optional: `DINKIQ_KEEP_RAW_DAYS` (original uploads are swept N days after ingest; default 14,
`0` = right away, negative = keep), `DINKIQ_DATA_DIR` (relocate all mutable state; tests set
it to a temp dir), `DINKIQ_HOST` (bind address, default `127.0.0.1`), `DINKIQ_LOG_DIR`.

## Layout

- `src/server.py` — app wiring: auth middleware, startup recovery, SPA pages, `/api/health`
- `src/api/` — routes by area: `uploads`, `sessions`, `trash`, `insights`, `exports`; `maintenance`
  (hourly sweeps); `common` (paths, `iter_sessions`, shared helpers)
- `src/pipeline.py` — storage layout only (paths, session ids, derived artifacts)
- `src/session_status.py` · `src/media.py` (ffmpeg, ingest, highlights) · `src/jobs.py` (worker,
  queue, recovery) · `src/stages.py` (events → shots → points → rating, `analyze()`) · `src/notify.py`
- CV/analysis: `tracking`, `ball`, `events`, `shots`, `points`, `court`, `metrics`, `dupr`, `feedback`
- `src/jsonio.py` — **all** JSON persistence goes through this (atomic writes, tolerant reads, NaN→null)
- `static/dashboard.html` (markup only) + `static/css/app.css` + `static/js/*.js` — **no build step**. The scripts are
  classic (non-module) and share one global scope, load order is the `<script>` order in the HTML (`core` first, `boot` last).
  `{{V}}` in the HTML is replaced per request by `webutil.asset_version()` (hash of css/js mtimes); `?v=` URLs are immutable-cached,
  everything else `no-cache`. Keep every URL relative / `BASE`-prefixed. `calgeom.js` is pure geometry, unit-tested under node
  (`tests/test_calgeom.py`). `sw.js` is the app-shell service worker: it must never cache `/api/`.
- Routes live in the URL hash (`#/`, `#/s/<id>`, `#/sessions`, `#/trends`, `#/compare?ids=`); `routeSession()` picks the screen a
  session's state calls for. Sample data exists only behind `?demo=1` (banner); a failed fetch shows the offline banner, never fake data.
- `src/webutil.py` — versioned static serving + gzip that skips media (gzip breaks Range seeking on clips)
- `src/calibrate_rubric.py` — fit rubric vs known-DUPR sessions
- `docs/API.md` — the API contract; `tests/test_api_docs.py` fails if it drifts from the app

## Commands

```bash
.venv/bin/python src/server.py          # Serve at http://127.0.0.1:8100
.venv/bin/python -m pytest tests/       # Run all test suites
.venv/bin/python src/calibrate_rubric.py # Fit rubric vs known DUPR sessions
```

Venv is Python 3.13 (torch incompatible with system 3.14); ffmpeg via brew.
The live service does not hot-reload Python: after changing `src/`, `launchctl kickstart -k gui/501/com.vantage.dinkiq`
(static files are read per request and need no restart).

## Invariants

- **`dupr.RUBRIC`** is the single tuning point; `models/rubric_calibrated.json` overrides anchors only
- **One decode pass**: ball detection and camera cuts fed from tracking loop; never add a second decode
- **`tracks.parquet` / `ball.parquet` are pixel-space** and survive recalibration; court-derived data is disposable
- **Frame numbers in parquets are REAL video frames** (tracking stride=2); time = frame / FPS
- **Homography**: outer corners user-estimated; kitchen corners add four correspondences to an UNWEIGHTED least-squares fit (they do not "anchor" it); ball valid only at bounces
- **Honest degradation**: gate shots on ball coverage; apply confidence penalties (short sample, geometry, camera cuts)
- **Single analysis worker** (`pipeline.enqueue_analyze`) — no parallel analyze threads (MPS contention)

## Gotchas

- PostToolUse hook runs `ruff check --fix` and DELETES unused imports — add imports in same edit as code
- **Repo lives at `~/dev/dinkiq`, deliberately outside iCloud Desktop.** iCloud once evicted ~25k files (mostly
  `.venv`) and the launchd service crash-looped on `Resource deadlock avoided`. Also: ultralytics silently strips
  `'` from weight paths, so never put the repo under a path containing one.
- **Tests never touch real data**: `tests/conftest.py` points `DINKIQ_DATA_DIR` at a temp dir and wipes it around
  every test. When monkeypatching, patch the name **on the module whose code looks it up** (`media.probe_video`,
  `notify.notify_webhook`, `jobs.enqueue_analyze`, `uploads.MAX_PARTIAL_UPLOADS`...); cross-module calls use the
  module attribute for exactly this reason. A patch on the wrong module is a silent no-op.
- Write JSON with `jsonio.atomic_write_json` and read with `jsonio.load_json`, never `write_text`/`json.loads` —
  one truncated file used to 500 every list endpoint, and a NaN used to 500 or crash iOS Safari.
- `py_compile` passes with missing imports — verify by importing the module
- Court lines are any color (not necessarily white)
- UI copy: DUPR estimates are "resembles ~X.X", never certified ratings — always with the range and confidence level
- **Tunable constants, each from ONE clip's evidence (the sample); re-check them against your own footage**:
  `events.SWING_MIN_BODY_HEIGHTS_S` (2.0), `events.Z_MIN` (10), `metrics.MIN_LIVE_SAMPLES` (~30 s),
  `shots.HIT_LIFT_MIN`/`HIT_RATE_MIN` (ball must follow hits 1.5x more than chance), `ball.MAX_SEGMENT_FRAMES`/
  `MIN_SEGMENT_*`. There is no labelled footage to validate any of them: the rating is an uncalibrated heuristic
  until `calibrate_rubric.py` has >= 4 known-rating sessions per match type.
- Pose runs on the GPU at imgsz 960 (`DINKIQ_DEVICE`, `DINKIQ_POSE_IMGSZ`); `tracks.meta.json` records what made the
  cache, and a different model/size discards it on re-analysis

