"""DinkIQ — FastAPI server: upload, calibrate, process, results, SPA."""

import asyncio
import base64
import datetime
import json
import logging
import math
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, Response)
from pydantic import BaseModel, Field, field_validator

import jobs
import logsetup
import media
import pipeline
import session_status
from jsonio import atomic_write_json, load_json, sanitize
from pipeline import SESSIONS, session_dir
from session_status import get_status
from tracking import MODEL_NAME as POSE_MODEL

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
ALLOWED_EXT = {".mp4", ".mov", ".m4v"}


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE per line, '#' comments) so secrets like
    DINKIQ_PASSWORD survive a server restart without depending on whatever
    shell/launcher started the process. Doesn't override an already-set
    real env var. No new dependency -- this is the only var that needs it."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


_load_dotenv(ROOT / ".env")

class SafeJSONResponse(JSONResponse):
    """Starlette's default refuses NaN/Inf (-> a 500 for the whole endpoint) and
    other JSON writers emit bare `NaN`, which iOS Safari hard-fails on. Map them
    to null once, here, for every response."""

    def render(self, content) -> bytes:
        return json.dumps(sanitize(content), ensure_ascii=False, allow_nan=False,
                          indent=None, separators=(",", ":")).encode("utf-8")


log = logging.getLogger("dinkiq")


RAW_KEEP_DAYS_DEFAULT = 14


def _raw_retention_s() -> float | None:
    """How long to keep a session's original upload once it has been ingested.
    DINKIQ_KEEP_RAW_DAYS: N days (default 14), 0 = drop as soon as ingest is done,
    negative = keep forever. Raw is only ever read DURING ingest; this is about
    whether you can get your original-quality footage back, hence a grace period."""
    try:
        days = float(os.environ.get("DINKIQ_KEEP_RAW_DAYS", RAW_KEEP_DAYS_DEFAULT))
    except ValueError:
        days = RAW_KEEP_DAYS_DEFAULT
    return None if days < 0 else days * 86400


def _sweep_raw() -> None:
    keep = _raw_retention_s()
    if keep is None or not SESSIONS.exists():
        return
    now = time.time()
    for d in SESSIONS.iterdir():
        if not d.is_dir() or d.name.startswith("."):
            continue
        # raw is the ONLY good copy until ingest has produced the normalized video,
        # and ingest/recovery read it while a job is running. A failed ingest can
        # leave a partial video.mp4, so look at the status, not just the file.
        status = get_status(d)
        ingest_unfinished = status.get("stage") == "ingest" and status.get("state") != "done"
        if jobs.is_busy(d) or ingest_unfinished or not (d / "video.mp4").exists():
            continue
        for raw in d.glob("raw.*"):
            try:
                if now - raw.stat().st_mtime >= keep:
                    raw.unlink(missing_ok=True)
                    log.info("retention: removed original upload %s/%s", d.name, raw.name)
            except OSError:
                log.exception("retention: could not remove %s", raw)


def _dir_bytes(d: Path) -> int:
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file())


def _maintenance_once() -> None:
    """Housekeeping that used to run only when someone happened to start an
    upload or open the trash view: abandoned partials and expired trash."""
    _sweep_uploads()
    _sweep_trash()
    _sweep_raw()


async def _maintenance_loop() -> None:
    while True:
        await asyncio.sleep(MAINTENANCE_INTERVAL_S)
        try:
            await asyncio.to_thread(_maintenance_once)
        except Exception:
            log.exception("maintenance pass failed (will retry)")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # The job queue is in memory only: repair whatever a restart/crash stranded
    # before serving. Must never stop the server from coming up.
    try:
        recovered = jobs.recover_interrupted()
        if any(recovered.values()):
            log.warning("startup recovery: %s", recovered)
    except Exception:
        log.exception("startup recovery failed (continuing)")
    try:
        _maintenance_once()
    except Exception:
        log.exception("startup maintenance failed (continuing)")
    task = asyncio.create_task(_maintenance_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="DinkIQ", default_response_class=SafeJSONResponse, lifespan=_lifespan)


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    # FastAPI's built-in handler uses the plain JSONResponse, and the error body
    # echoes the offending `input` -- so a NaN in a request turned a clean 422
    # into a 500. Same body shape, routed through the NaN-safe response.
    return SafeJSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})

# ── Optional HTTP Basic Auth for remote access (e.g. via Tailscale Funnel) ──
# Set DINKIQ_PASSWORD=somepassword (in .env, or the environment directly).
#
# A request skips the password only if it is
#   (a) from the reverse proxy -- carries DINKIQ_PROXY_SECRET in X-Dinkiq-Proxy
#       (Caddy overwrites that header on every proxied request, after its own
#       basic_auth has passed), or
#   (b) genuinely local -- loopback peer, no forwarding headers, loopback Host.
# The Host header alone is NOT trusted: any client picks its own, and the
# server used to listen on every interface, so `Host: 127.0.0.1` from anywhere
# on the LAN/tailnet got full access. (b) still checks Host, but only to keep
# DNS-rebinding pages (their own name resolving to 127.0.0.1) out.
DEFAULT_HOST = "127.0.0.1"  # Caddy proxies to 127.0.0.1:8100; nothing else needs the port
HOST = os.environ.get("DINKIQ_HOST", "").strip() or DEFAULT_HOST
_DINKIQ_PASSWORD = os.environ.get("DINKIQ_PASSWORD", "").strip() or None
_DINKIQ_PASSWORD_BYTES = _DINKIQ_PASSWORD.encode() if _DINKIQ_PASSWORD else None
_PROXY_SECRET = os.environ.get("DINKIQ_PROXY_SECRET", "").strip() or None
_PROXY_SECRET_BYTES = _PROXY_SECRET.encode() if _PROXY_SECRET else None

_LOCAL_PEERS = ("127.0.0.1", "::1")
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")
_FORWARDING_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto",
                       "x-real-ip", "forwarded")


def _host_without_port(host: str) -> str:
    host = host.strip().lower()
    if host.startswith("["):  # [::1]:8100
        return host[:host.find("]") + 1] if "]" in host else host
    return host.rsplit(":", 1)[0] if ":" in host else host


def _from_trusted_proxy(request: Request) -> bool:
    if _PROXY_SECRET_BYTES is None:
        return False
    provided = request.headers.get("x-dinkiq-proxy", "").encode()
    return secrets.compare_digest(provided, _PROXY_SECRET_BYTES)


def _is_local_request(request: Request) -> bool:
    peer = request.client.host if request.client else ""
    if peer not in _LOCAL_PEERS:
        return False
    if any(h in request.headers for h in _FORWARDING_HEADERS):
        return False
    return _host_without_port(request.headers.get("host", "")) in _LOCAL_HOSTS


@app.middleware("http")
async def _basic_auth(request, call_next):
    if _DINKIQ_PASSWORD_BYTES is None:
        return await call_next(request)
    if _from_trusted_proxy(request) or _is_local_request(request):
        return await call_next(request)
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header[6:]).split(b":", 1)
            provided = decoded[1] if len(decoded) == 2 else b""
            if secrets.compare_digest(provided, _DINKIQ_PASSWORD_BYTES):
                return await call_next(request)
        except Exception:
            pass
    return PlainTextResponse("Unauthorized", status_code=401,
                             headers={"WWW-Authenticate": 'Basic realm="DinkIQ"'})


COORD_LIMIT_PX = 20_000.0  # frames are ~1280x720 and the UI pads 25%; this only stops garbage


def _check_point(pt: list[float]) -> None:
    if len(pt) != 2 or not all(math.isfinite(v) and abs(v) <= COORD_LIMIT_PX for v in pt):
        raise ValueError("each point must be [x, y], finite, and within a sane pixel range")


class Calibration(BaseModel):
    corners_px: list[list[float]] = Field(min_length=4, max_length=4)
    kitchen_px: list[list[float]] | None = Field(default=None, min_length=4, max_length=4)
    self_px: list[float] = Field(min_length=2, max_length=2)
    partner_px: list[float] | None = Field(default=None, min_length=2, max_length=2)

    # NaN/Infinity pass plain `float`, then poison the homography and every
    # JSON file derived from it (and last_calibration.json, which persists)
    @field_validator("corners_px", "kitchen_px")
    @classmethod
    def _valid_points(cls, v):
        for pt in v or []:
            _check_point(pt)
        return v

    @field_validator("self_px", "partner_px")
    @classmethod
    def _valid_point(cls, v):
        if v is not None:
            _check_point(v)
        return v


class BulkDelete(BaseModel):
    ids: list[str] = Field(min_length=1)


SESSION_CONTEXTS = {"practice", "league", "tournament"}


class MetaPatch(BaseModel):
    label: str | None = Field(default=None, max_length=120)
    played_at: str | None = Field(default=None, max_length=10)  # YYYY-MM-DD
    known_dupr: float | None = Field(default=None, ge=2.0, le=8.0)
    notes: str | None = Field(default=None, max_length=500)
    opponent: str | None = Field(default=None, max_length=80)
    player: str | None = Field(default=None, max_length=80)
    context: str | None = Field(default=None)

    @field_validator("context")
    @classmethod
    def _valid_context(cls, v):
        if v is not None and v not in SESSION_CONTEXTS:
            raise ValueError(f"context must be one of {sorted(SESSION_CONTEXTS)}")
        return v

    @field_validator("played_at")
    @classmethod
    def _valid_date(cls, v):
        # "" is how the form clears the field; anything else must be a real
        # calendar date. This string is rendered into the sessions table, so
        # free text here was an HTML-injection vector.
        if v is None or v == "":
            return v
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            raise ValueError("played_at must be YYYY-MM-DD")
        datetime.date.fromisoformat(v)  # rejects 2026-13-45
        return v


def _mount_prefix(request: Request) -> str:
    """Path prefix this app is mounted under, from the reverse proxy.

    Served directly (localhost, LAN) this is "" and every URL stays
    root-absolute as before. Behind the vantage Caddy hub the app lives at
    /app/dinkiq/ with the prefix stripped before it reaches us, so the
    server never sees it in request.url — without echoing the proxy's
    X-Forwarded-Prefix back to the page, the frontend's root-absolute
    fetch('/api/...') calls resolve against the hub origin instead of this
    app and every one of them 404s.
    """
    return (request.headers.get("x-forwarded-prefix") or "").rstrip("/")


def _spa(path: Path, request: Request) -> HTMLResponse:
    """Serve an SPA page with window.__BASE__ set to the mount prefix."""
    html = path.read_text()
    prefix = _mount_prefix(request)
    if prefix:
        tag = f"<script>window.__BASE__={json.dumps(prefix)};</script>"
        html = html.replace("<head>", "<head>" + tag, 1)
    return HTMLResponse(html)


@app.get("/")
def index(request: Request):
    return _spa(STATIC / "dashboard.html", request)


@app.get("/backyard")
def backyard(request: Request):
    # self-contained page, no API calls of its own — no prefix needed, and it
    # rebuilds <head> at runtime anyway (see its own mobile-fix comment)
    return FileResponse(STATIC / "backyard.html")


STARTED_AT = time.time()


def _weights_present() -> bool:
    return (pipeline.MODELS / POSE_MODEL).exists()


def _data_dir_writable() -> bool:
    try:
        SESSIONS.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=SESSIONS.parent):
            return True
    except OSError:
        return False


def _tool_check(name: str) -> dict:
    found = shutil.which(name)
    return {"ok": bool(found),
            "detail": found or f"{name} not found on PATH — analysis would fail (launchd's PATH excludes Homebrew)"}


@app.get("/api/health")
def health():
    """Is this server able to do its job? 200 only when every check passes, so a
    watchdog can alert on the status code alone. The old failure mode was a server
    that answered requests perfectly while every analysis died on a missing tool."""
    free = _free_bytes()
    worker = jobs.worker_status()
    writable = _data_dir_writable()
    weights = _weights_present()
    checks = {
        "ffmpeg": _tool_check("ffmpeg"),
        "ffprobe": _tool_check("ffprobe"),
        "disk": {"ok": free >= MIN_FREE_BYTES, "detail": f"{free / (1 << 30):.1f} GB free"},
        "data_dir": {"ok": writable, "detail": "writable" if writable else "not writable"},
        "weights": {"ok": weights, "detail": f"{POSE_MODEL} {'present' if weights else 'MISSING'}"},
        "worker": {"ok": worker["alive"] is not False,
                   "detail": "not started yet" if not worker["started"] else
                   ("running" if worker["alive"] else "DEAD — restart the service")},
    }
    ok = all(c["ok"] for c in checks.values())
    sessions_bytes = sum(_dir_bytes(d) for d in SESSIONS.iterdir()
                         if d.is_dir() and not d.name.startswith(".")) if SESSIONS.exists() else 0
    return SafeJSONResponse(status_code=200 if ok else 503, content={
        "ok": ok, "checks": checks, "uptime_s": round(time.time() - STARTED_AT),
        "sessions_bytes": sessions_bytes,
        "queue": {"queued": worker["queued"], "running": worker["running"]}})


MAX_UPLOAD_BYTES = 2 << 30  # 2 GB
WARN_DURATION_S = 30 * 60


MATCH_TYPES = {"singles", "doubles"}

# ── Resumable upload ──────────────────────────────────────────────────────
# A phone on cellular pushing a multi-GB clip through a single POST loses
# the entire transfer on one dropped connection. These three endpoints let
# the client append in chunks and, after a drop, ask where to pick up.
UPLOADS = SESSIONS.parent / "uploads"
UPLOAD_TTL_S = 24 * 3600
MIN_FREE_BYTES = 2 << 30          # keep this much disk free; below it, refuse uploads
MAX_PARTIAL_UPLOADS = 6           # open resumable uploads at once
STALE_PARTIAL_S = 3600            # an idle partial this old may be reclaimed to make room
MAINTENANCE_INTERVAL_S = 3600
# One writer per upload. A phone that times out retries a chunk while the server
# is still receiving the first attempt; without this both pass the offset check
# and both append, duplicating bytes.
_upload_locks: dict[str, asyncio.Lock] = {}


def _upload_part(upload_id: str) -> Path:
    """Partial-upload path for an id, rejecting anything not our own uuid
    hex (this lands in a filesystem path, so never trust the client's)."""
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id or ""):
        raise HTTPException(400, "bad upload id")
    return UPLOADS / f"{upload_id}.part"


def _sweep_uploads() -> None:
    """Drop abandoned partials so a failed 2 GB upload can't sit forever."""
    if not UPLOADS.exists():
        return
    now = time.time()
    for f in UPLOADS.iterdir():
        if f.is_file() and now - f.stat().st_mtime > UPLOAD_TTL_S:
            f.unlink(missing_ok=True)
            _upload_locks.pop(f.stem, None)


class UploadInit(BaseModel):
    filename: str = Field(max_length=260)
    match_type: str = "singles"
    size: int | None = Field(default=None, ge=0)  # declared bytes; lets us refuse early
    session_id: str | None = None                  # set when replacing a session's video

    @field_validator("match_type")
    @classmethod
    def _valid_match_type(cls, v):
        if v not in MATCH_TYPES:
            raise ValueError(f"match_type must be one of {sorted(MATCH_TYPES)}")
        return v


def _free_bytes() -> int:
    UPLOADS.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(UPLOADS).free


def _open_partials() -> list[Path]:
    if not UPLOADS.exists():
        return []
    return sorted(UPLOADS.glob("*.part"), key=lambda f: f.stat().st_mtime)


def _reserve_partial_slot() -> None:
    """Refuse when too many uploads are open -- but first reclaim abandoned ones,
    oldest first, so a phone that gave up mid-upload can't lock you out. An
    upload that is still receiving chunks is never touched."""
    parts = _open_partials()
    while len(parts) >= MAX_PARTIAL_UPLOADS and time.time() - parts[0].stat().st_mtime > STALE_PARTIAL_S:
        stale = parts.pop(0)
        stale.unlink(missing_ok=True)
        _upload_locks.pop(stale.stem, None)
    if len(parts) >= MAX_PARTIAL_UPLOADS:
        raise HTTPException(429, "too many uploads in progress — finish or wait for one to expire")


@app.post("/api/upload/init")
def upload_init(req: UploadInit):
    ext = Path(req.filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"unsupported file type {ext}")
    if req.session_id is not None:  # replacing a video: fail now, not after 2 GB
        _refuse_if_busy(_sdir(req.session_id))
    if req.size is not None and req.size > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "upload exceeds 2 GB limit")
    free = _free_bytes()
    if free - (req.size or 0) < MIN_FREE_BYTES:
        raise HTTPException(507, f"not enough disk space on the server ({free / (1 << 30):.1f} GB free)")
    _sweep_uploads()
    _reserve_partial_slot()
    UPLOADS.mkdir(parents=True, exist_ok=True)
    upload_id = uuid.uuid4().hex
    _upload_part(upload_id).touch()
    return {"upload_id": upload_id, "offset": 0}


@app.get("/api/upload/{upload_id}")
def upload_status(upload_id: str):
    """Bytes already stored — the client resumes from here after a drop."""
    part = _upload_part(upload_id)
    if not part.exists():
        raise HTTPException(404, "unknown upload")
    return {"upload_id": upload_id, "offset": part.stat().st_size}


@app.put("/api/upload/{upload_id}")
async def upload_chunk(upload_id: str, request: Request, offset: int = 0):
    """Append one chunk. `offset` must equal what we already hold, so a
    retried or out-of-order chunk can never silently corrupt the file."""
    part = _upload_part(upload_id)
    async with _upload_locks.setdefault(upload_id, asyncio.Lock()):
        if not part.exists():
            raise HTTPException(404, "unknown upload")
        have = part.stat().st_size
        if offset != have:
            # not an error the client can't recover from — tell it where we are
            raise HTTPException(409, f"offset mismatch: server has {have}")
        if _free_bytes() < MIN_FREE_BYTES:
            raise HTTPException(507, "server disk is nearly full — upload stopped; free some space and resume")
        with part.open("ab") as f:
            async for chunk in request.stream():
                have += len(chunk)
                if have > MAX_UPLOAD_BYTES:
                    f.close()
                    part.unlink(missing_ok=True)
                    raise HTTPException(413, "upload exceeds 2 GB limit")
                f.write(chunk)
    return {"upload_id": upload_id, "offset": have}


class UploadFinish(BaseModel):
    filename: str = Field(max_length=260)
    match_type: str = "singles"
    session_id: str | None = None  # set when this upload replaces a session's video

    @field_validator("match_type")
    @classmethod
    def _valid_match_type(cls, v):
        if v not in MATCH_TYPES:
            raise ValueError(f"match_type must be one of {sorted(MATCH_TYPES)}")
        return v


@app.post("/api/upload/{upload_id}/finish")
def upload_finish(upload_id: str, req: UploadFinish):
    """Promote a completed partial into a real session and start ingest."""
    part = _upload_part(upload_id)
    if not part.exists():
        raise HTTPException(404, "unknown upload")
    ext = Path(req.filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"unsupported file type {ext}")
    if part.stat().st_size == 0:
        part.unlink(missing_ok=True)
        raise HTTPException(400, "no data uploaded")

    if req.session_id is not None:
        sdir = _sdir(req.session_id)
        _refuse_if_busy(sdir)  # may have started processing since init
        incoming = sdir / f"incoming{ext}"
        shutil.move(str(part), str(incoming))
        _upload_locks.pop(upload_id, None)
        return _replace_video(sdir, incoming, req.filename)

    sid = uuid.uuid4().hex[:12]
    sdir = SESSIONS / sid
    sdir.mkdir(parents=True)
    raw = sdir / f"raw{ext}"
    shutil.move(str(part), str(raw))
    try:
        result = _start_session(sdir, raw, req.filename, req.match_type)
    except media.ToolMissing as e:
        # don't leave a session with no meta/status behind; keep the upload so
        # the user can simply retry once the PATH problem is fixed
        shutil.move(str(raw), str(part))
        shutil.rmtree(sdir, ignore_errors=True)
        raise HTTPException(503, str(e))
    _upload_locks.pop(upload_id, None)
    return result


# Analysis runs at 720p30 no matter what comes in, so anything larger is
# paid for twice (upload bytes, then transcode) and thrown away. Measured on
# a 20s clip: 4K60 took 4.0s to ingest and 5.2 MB, vs 1.7s and 1.5 MB for
# 1080p30 — ~2.4x the ingest and ~3.5x the upload for identical results.
CAPTURE_HINT_HEIGHT = 1080
CAPTURE_HINT_FPS = 31.0


def capture_hint(info: dict) -> str | None:
    """Suggest a leaner capture setting when the source is bigger than the
    pipeline can use. None when the footage is already reasonable."""
    too_tall = info.get("height", 0) > CAPTURE_HINT_HEIGHT
    too_fast = info.get("fps", 0) > CAPTURE_HINT_FPS
    if not (too_tall or too_fast):
        return None
    was = f"{info.get('height', 0)}p"
    if info.get("fps"):
        was += f"{info['fps']:.0f}"
    return (f"Recorded at {was}. DinkIQ analyzes at 720p30, so a smaller "
            "capture uploads faster and starts sooner with identical results — "
            "1080p30 is the sweet spot.")


def _start_session(sdir: Path, raw: Path, filename: str, match_type: str) -> dict:
    """Write meta, kick off ingest — shared by both upload paths."""
    meta = {"filename": filename, "match_type": match_type}
    info = media.probe_video(raw)
    dur = info.get("duration", 0.0)
    if dur > WARN_DURATION_S:
        meta["warning"] = (f"video is {dur/60:.0f} min — analysis will take a "
                           "while; consider splitting into games")
    hint = capture_hint(info)
    if hint:
        meta["capture_hint"] = hint
    atomic_write_json(sdir / "meta.json", meta)
    threading.Thread(target=media.ingest, args=(sdir, raw), daemon=True).start()
    return {"session_id": sdir.name, "duration_s": dur,
            "warning": meta.get("warning"), "capture_hint": hint}


def _refuse_if_busy(sdir: Path) -> None:
    if jobs.is_busy(sdir):
        raise HTTPException(409, "session is still processing")


def _replace_video(sdir: Path, incoming: Path, filename: str) -> dict:
    """Swap `incoming` in as the session's source video -- but only once it is
    known to be a readable video. The original raw file and all analysis are
    untouched until that check passes (this used to delete them first, so a bad
    replacement left a session with nothing)."""
    try:
        info = media.probe_video(incoming)
    except media.ToolMissing as e:
        incoming.unlink(missing_ok=True)  # the original is untouched
        raise HTTPException(503, str(e))
    if not info.get("codec") or info.get("duration", 0) <= 0:
        incoming.unlink(missing_ok=True)
        raise HTTPException(400, "the replacement isn't a readable video — the original was kept")
    for old in sdir.glob("raw.*"):
        old.unlink(missing_ok=True)
    raw = sdir / f"raw{incoming.suffix.lower()}"
    os.replace(incoming, raw)
    pipeline.clear_derived(sdir)
    (sdir / "calibration.json").unlink(missing_ok=True)
    for name in ("video.mp4", "audio.wav", "frame0.jpg", "tracks.parquet",
                 "ball.parquet", "cuts.json", "ingest.json"):
        (sdir / name).unlink(missing_ok=True)
    meta = load_json(sdir / "meta.json")
    meta["filename"] = filename
    meta.pop("warning", None)
    meta.pop("capture_hint", None)
    dur = info.get("duration", 0.0)
    if dur > WARN_DURATION_S:
        meta["warning"] = (f"video is {dur/60:.0f} min — analysis will take a "
                           "while; consider splitting into games")
    hint = capture_hint(info)
    if hint:
        meta["capture_hint"] = hint
    atomic_write_json(sdir / "meta.json", meta)
    session_status.set_status(sdir, "ingest", "running")  # not the previous run's "done"
    threading.Thread(target=media.ingest, args=(sdir, raw), daemon=True).start()
    return {"session_id": sdir.name, "duration_s": dur,
            "warning": meta.get("warning"), "capture_hint": hint}


TRASH = SESSIONS / ".trash"
TRASH_RETENTION_S = 7 * 24 * 3600


@app.get("/api/sessions")
def sessions(label: str | None = None, opponent: str | None = None,
            date_from: str | None = None, date_to: str | None = None,
            context: str | None = None):
    """Session list, newest first. Optional case-insensitive substring filters
    on label/opponent, an inclusive played_at date range (YYYY-MM-DD), and an
    exact match on context (practice/league/tournament)."""
    out = []
    if SESSIONS.exists():
        for d in sorted(SESSIONS.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not d.is_dir() or d == TRASH:
                continue
            meta_f = d / "meta.json"
            meta = load_json(meta_f)
            if label and label.lower() not in (meta.get("label") or "").lower():
                continue
            if opponent and opponent.lower() not in (meta.get("opponent") or "").lower():
                continue
            if context and meta.get("context") != context:
                continue
            played_at = meta.get("played_at")
            if date_from and (not played_at or played_at < date_from):
                continue
            if date_to and (not played_at or played_at > date_to):
                continue
            status = get_status(d)
            row = {
                "session_id": d.name,
                "filename": meta.get("filename"),
                "label": meta.get("label"),
                "played_at": meta.get("played_at"),
                "known_dupr": meta.get("known_dupr"),
                "notes": meta.get("notes"),
                "opponent": meta.get("opponent"),
                "player": meta.get("player"),
                "context": meta.get("context"),
                "disk_bytes": _dir_bytes(d),
                "uploaded_at": d.stat().st_mtime,
                **status,
            }
            if status.get("stage") == "queued":
                row["queue_position"] = jobs.queue_position(d)
            if status.get("stage") == "done":
                # summary fields the sessions list/table renders directly —
                # real numbers, not a UI placeholder, for every completed row
                m = _load_json(d, "metrics.json")
                ev = _load_json(d, "events.json")
                dp = _load_json(d, "dupr.json")
                row["kitchen_pct"] = m.get("zone_pct", {}).get("kitchen")
                row["avg_rally_hits"] = ev.get("avg_rally_hits")
                row["dupr_band"] = dp.get("band")
                row["dupr_confidence"] = dp.get("confidence")
            out.append(row)
    return out


@app.get("/api/sessions/export.csv")
def export_sessions_csv():
    """Flat CSV of every completed session's summary stats (same fields
    _compare_summary already aggregates), for users who want to pivot their
    own history in Sheets/Excel."""
    import csv
    import io

    rows = []
    if SESSIONS.exists():
        for d in sorted(SESSIONS.iterdir(), key=lambda p: p.stat().st_mtime):
            if not d.is_dir() or d == TRASH:
                continue
            if get_status(d).get("stage") != "done":
                continue
            row = _compare_summary(d)
            row["player"] = _load_json(d, "meta.json").get("player") or DEFAULT_PLAYER_NAME
            rows.append(row)

    def _csv_safe(v):
        # a cell starting with = + - @ is run as a formula by Excel/Sheets;
        # label/opponent/player are free text (and shared users can set them)
        return "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v

    rows = [{k: _csv_safe(v) for k, v in row.items()} for row in rows]
    buf = io.StringIO()
    fieldnames = ["session_id", "player", "label", "played_at", "opponent",
                 "kitchen_pct", "transition_pct", "distance_ft", "avg_speed_ft_s",
                 "coverage_pct", "rally_count", "avg_rally_hits", "play_time_pct",
                 "points_won", "points_lost", "win_pct", "dupr_band", "dupr_confidence"]
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return Response(content=buf.getvalue(), media_type="text/csv", headers={
        "Content-Disposition": 'attachment; filename="dinkiq_sessions.csv"'})


@app.get("/api/progress")
def progress():
    """Cross-session trend series (completed sessions, oldest first)."""
    points = []
    if SESSIONS.exists():
        for d in SESSIONS.iterdir():
            if not d.is_dir() or get_status(d).get("stage") != "done":
                continue
            m_f, e_f, dp_f, meta_f = (d / "metrics.json", d / "events.json",
                                      d / "dupr.json", d / "meta.json")
            m = load_json(m_f)
            if not m:  # missing or unreadable: nothing to chart for this session
                continue
            ev = load_json(e_f)
            dp = load_json(dp_f)
            meta = load_json(meta_f)
            date = meta.get("played_at") or datetime.date.fromtimestamp(
                d.stat().st_mtime).isoformat()
            drills = dp.get("drills") or []
            top_drill = drills[0] if drills else {}
            points.append({
                "session_id": d.name,
                "label": meta.get("label") or meta.get("filename"),
                "date": date,
                "kitchen_pct": m.get("zone_pct", {}).get("kitchen"),
                "transition_pct": m.get("zone_pct", {}).get("transition"),
                "distance_ft": m.get("distance_ft"),
                "avg_rally_hits": ev.get("avg_rally_hits"),
                "dupr_band": dp.get("band"),
                "dupr_confidence": dp.get("confidence"),
                "known_dupr": meta.get("known_dupr"),
                "weakest_dimension": top_drill.get("dimension"),
                "weakest_label": top_drill.get("target_label"),
                # per-dimension bands, so the UI can tell whether a
                # previously-flagged weak dimension has actually improved
                "dimensions": dp.get("dimensions") or {},
            })
    points.sort(key=lambda p: p["date"])
    return points


@app.patch("/api/session/{sid}/meta")
def patch_meta(sid: str, patch: MetaPatch):
    sdir = _sdir(sid)
    meta_f = sdir / "meta.json"
    meta = load_json(meta_f)
    for k, v in patch.model_dump(exclude_none=True).items():
        meta[k] = v
    atomic_write_json(meta_f, meta)
    return meta


WINNER_VALUES = {"my_team", "opp_team", "unknown"}


class PointCorrection(BaseModel):
    winner: str
    unforced_error: bool | None = None

    @field_validator("winner")
    @classmethod
    def _valid_winner(cls, v):
        if v not in WINNER_VALUES:
            raise ValueError(f"winner must be one of {sorted(WINNER_VALUES)}")
        return v


@app.patch("/api/session/{sid}/points/{idx}")
def correct_point(sid: str, idx: int, patch: PointCorrection):
    """Manually override a heuristically-detected rally winner (the
    audio+ball heuristic in points.py can misjudge close calls). Recomputes
    the session's win/loss summary from the corrected outcomes; per-player
    hit counts are independent of who won each rally, so they're carried
    over unchanged rather than recomputed (the hitter attribution used to
    build them isn't persisted anywhere)."""
    from points import point_summary, serve_side_win_rates, shot_type_outcomes

    sdir = _sdir(sid)
    pf = sdir / "points.json"
    data = load_json(pf)
    outcomes = data.get("outcomes", [])
    if idx < 0 or idx >= len(outcomes):
        raise HTTPException(404, "rally index out of range")
    outcomes[idx]["winner"] = patch.winner
    if patch.unforced_error is not None:
        outcomes[idx]["unforced_error"] = patch.unforced_error
    outcomes[idx]["corrected"] = True
    ball_available = "caveat" not in data
    summary = point_summary(outcomes, ball_available)
    summary["hits_by_player"] = data.get("hits_by_player", {})
    # a corrected winner changes who "gets credit" for a rally, so the
    # serve-placement/shot-type breakdowns (both derived from outcomes)
    # must be recomputed too, not just carried over stale
    shots_report = _load_json(sdir, "shots.json")
    rallies = _load_json(sdir, "events.json").get("rallies", [])
    summary["serve_side_win_rates"] = serve_side_win_rates(
        shots_report.get("serves") or [], outcomes)
    summary["shot_type_outcomes"] = shot_type_outcomes(
        shots_report.get("shots") or [], rallies, outcomes)
    new_data = {"outcomes": outcomes, **summary}
    atomic_write_json(pf, new_data)
    return new_data


@app.post("/api/sessions/delete")
def delete_sessions(req: BulkDelete):
    """Soft-delete: move each session into .trash/ (restorable) rather than
    deleting outright. Trash is purged lazily (see _sweep_trash) after
    TRASH_RETENTION_S."""
    deleted, errors = [], []
    for sid in req.ids:
        try:
            d = session_dir(sid)  # rejects path traversal
        except ValueError:
            errors.append({"id": sid, "error": "bad session id"})
            continue
        if not d.exists():
            errors.append({"id": sid, "error": "not found"})
            continue
        if jobs.is_busy(d):  # moving the folder out from under a running job breaks it
            errors.append({"id": sid, "error": "session is still processing"})
            continue
        meta_f = d / "meta.json"
        meta = load_json(meta_f)
        meta["deleted_at"] = time.time()
        atomic_write_json(meta_f, meta)
        TRASH.mkdir(parents=True, exist_ok=True)
        dest = TRASH / sid
        if dest.exists():  # stale leftover from a prior delete of the same id
            shutil.rmtree(dest)
        shutil.move(str(d), str(dest))
        deleted.append(sid)
    return {"deleted": deleted, "errors": errors}


def _sweep_trash() -> None:
    """Purge trashed sessions older than the retention window."""
    if not TRASH.exists():
        return
    now = time.time()
    for d in TRASH.iterdir():
        if not d.is_dir():
            continue
        meta_f = d / "meta.json"
        meta = load_json(meta_f)
        deleted_at = meta.get("deleted_at", 0)
        if now - deleted_at > TRASH_RETENTION_S:
            shutil.rmtree(d, ignore_errors=True)


@app.get("/api/sessions/trash")
def list_trash():
    _sweep_trash()
    out = []
    if TRASH.exists():
        for d in sorted(TRASH.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not d.is_dir():
                continue
            meta = _load_json(d, "meta.json")
            out.append({
                "session_id": d.name,
                "label": meta.get("label"),
                "filename": meta.get("filename"),
                "deleted_at": meta.get("deleted_at"),
            })
    return out


@app.post("/api/sessions/restore")
def restore_sessions(req: BulkDelete):
    restored, errors = [], []
    for sid in req.ids:
        try:
            session_dir(sid)  # validates id shape, rejects traversal
        except ValueError:
            errors.append({"id": sid, "error": "bad session id"})
            continue
        d = TRASH / sid
        if not d.exists():
            errors.append({"id": sid, "error": "not found in trash"})
            continue
        meta_f = d / "meta.json"
        meta = load_json(meta_f)
        meta.pop("deleted_at", None)
        atomic_write_json(meta_f, meta)
        dest = SESSIONS / sid
        if dest.exists():  # stale leftover from a prior restore of the same id
            shutil.rmtree(dest)
        shutil.move(str(d), str(dest))
        restored.append(sid)
    return {"restored": restored, "errors": errors}


def _sdir(sid: str) -> Path:
    try:
        d = session_dir(sid)
    except ValueError:
        raise HTTPException(400, "bad session id")
    if not d.exists():
        raise HTTPException(404, "session not found")
    return d


def _load_json(sdir: Path, name: str) -> dict:
    return load_json(sdir / name)


def _compare_summary(sdir: Path) -> dict:
    m = _load_json(sdir, "metrics.json")
    ev = _load_json(sdir, "events.json")
    pts = _load_json(sdir, "points.json")
    dp = _load_json(sdir, "dupr.json")
    meta = _load_json(sdir, "meta.json")
    zp = m.get("zone_pct", {})
    return {
        "session_id": sdir.name,
        "label": meta.get("label") or meta.get("filename") or sdir.name,
        "played_at": meta.get("played_at"),
        "opponent": meta.get("opponent"),
        "kitchen_pct": zp.get("kitchen"),
        "transition_pct": zp.get("transition"),
        "distance_ft": m.get("distance_ft"),
        "avg_speed_ft_s": m.get("avg_speed_ft_s"),
        "coverage_pct": m.get("coverage_pct"),
        "rally_count": ev.get("rally_count"),
        "avg_rally_hits": ev.get("avg_rally_hits"),
        "play_time_pct": ev.get("play_time_pct"),
        "points_won": pts.get("points_won"),
        "points_lost": pts.get("points_lost"),
        "win_pct": pts.get("win_pct"),
        "dupr_band": dp.get("band"),
        "dupr_confidence": dp.get("confidence"),
    }


NON_METRIC_FIELDS = {"session_id", "label", "played_at", "opponent"}


@app.get("/api/compare")
def compare(a: str, b: str):
    da, db = _sdir(a), _sdir(b)
    for d in (da, db):
        if get_status(d).get("stage") != "done":
            raise HTTPException(409, f"session {d.name} is not ready to compare")
    sa, sb = _compare_summary(da), _compare_summary(db)
    diffs = {}
    for k in sa:
        if k in NON_METRIC_FIELDS:
            continue
        va, vb = sa.get(k), sb.get(k)
        delta = round(vb - va, 2) if isinstance(va, (int, float)) and isinstance(vb, (int, float)) else None
        diffs[k] = {"a": va, "b": vb, "delta": delta}
    return {"a": sa, "b": sb, "diffs": diffs}


@app.get("/api/compare/multi")
def compare_multi(ids: str):
    """Compare 3+ sessions side by side (no pairwise deltas — just summaries).

    ids is a comma-separated list of session ids, e.g. ?ids=a,b,c.
    """
    sids = [s for s in ids.split(",") if s]
    if len(sids) < 2:
        raise HTTPException(400, "provide at least 2 session ids")
    dirs = [_sdir(s) for s in sids]
    for d in dirs:
        if get_status(d).get("stage") != "done":
            raise HTTPException(409, f"session {d.name} is not ready to compare")
    return {"sessions": [_compare_summary(d) for d in dirs]}


@app.get("/api/opponents/{name}/history")
def opponent_history(name: str):
    """Aggregate win/loss record + per-session summaries against one named
    opponent (case-insensitive exact match on the session's `opponent`
    meta field), newest first."""
    sessions_out = []
    if SESSIONS.exists():
        for d in sorted(SESSIONS.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not d.is_dir() or d == TRASH:
                continue
            meta = _load_json(d, "meta.json")
            if (meta.get("opponent") or "").strip().lower() != name.strip().lower():
                continue
            if get_status(d).get("stage") != "done":
                continue
            sessions_out.append(_compare_summary(d))
    known = [s for s in sessions_out if s.get("win_pct") is not None]
    wins = sum(1 for s in known if s["points_won"] > s["points_lost"])
    losses = sum(1 for s in known if s["points_lost"] > s["points_won"])
    return {
        "opponent": name,
        "matches": len(sessions_out),
        "wins": wins,
        "losses": losses,
        "win_pct": round(100.0 * wins / len(known), 1) if known else None,
        "sessions": sessions_out,
    }


@app.get("/api/opponents/{name}/scouting")
def opponent_scouting(name: str):
    """Aggregate this opponent's own shot-landing tendency (dominant side,
    from tracked bounces after their hits — see shots.py's opponent_shots)
    across every completed session played against them, plus the win/loss
    context from opponent_history. Real-data only: sessions with no ball
    tracking simply don't contribute landing data."""
    side_counts: dict[str, int] = {}
    shots_tracked = 0
    if SESSIONS.exists():
        for d in SESSIONS.iterdir():
            if not d.is_dir() or d == TRASH:
                continue
            meta = _load_json(d, "meta.json")
            if (meta.get("opponent") or "").strip().lower() != name.strip().lower():
                continue
            if get_status(d).get("stage") != "done":
                continue
            opp = _load_json(d, "shots.json").get("opponent_shots") or {}
            for side, n in (opp.get("side_counts") or {}).items():
                side_counts[side] = side_counts.get(side, 0) + n
            shots_tracked += opp.get("shots_tracked", 0)

    record = opponent_history(name)
    dominant = max(side_counts, key=side_counts.get) if side_counts else None
    return {
        "opponent": name,
        "matches": record["matches"],
        "wins": record["wins"],
        "losses": record["losses"],
        "win_pct": record["win_pct"],
        "shots_tracked": shots_tracked,
        "side_counts": side_counts,
        "dominant_side": dominant,
        "dominant_side_pct": round(100.0 * side_counts[dominant] / shots_tracked, 1)
                            if dominant and shots_tracked else None,
    }


DEFAULT_PLAYER_NAME = "You"


@app.get("/api/streak")
def streak():
    """Consecutive-day play streak + this-week count, scoped to
    DEFAULT_PLAYER_NAME sessions (same "your" scoping as the leaderboard's
    unset-player bucket) — a session tagged with someone else's `player`
    shouldn't inflate your own streak."""
    dates: set[datetime.date] = set()
    if SESSIONS.exists():
        for d in SESSIONS.iterdir():
            if not d.is_dir() or d == TRASH:
                continue
            if get_status(d).get("stage") != "done":
                continue
            meta = _load_json(d, "meta.json")
            if (meta.get("player") or "").strip() and meta["player"].strip() != DEFAULT_PLAYER_NAME:
                continue
            played_at = meta.get("played_at")
            if played_at:
                try:
                    dates.add(datetime.date.fromisoformat(played_at))
                    continue
                except ValueError:
                    pass
            dates.add(datetime.date.fromtimestamp(d.stat().st_mtime))

    today = datetime.date.today()
    one_day = datetime.timedelta(days=1)

    def run_forward_from(start: datetime.date) -> int:
        n, cur = 0, start
        while cur in dates:
            n += 1
            cur += one_day
        return n

    def run_back_from(start: datetime.date) -> int:
        n, cur = 0, start
        while cur in dates:
            n += 1
            cur -= one_day
        return n

    current = run_back_from(today) or run_back_from(today - one_day)
    longest = max((run_forward_from(dt) for dt in dates if dt - one_day not in dates), default=0)
    week_ago = today - datetime.timedelta(days=6)
    sessions_this_week = sum(1 for dt in dates if week_ago <= dt <= today)

    return {
        "current_streak_days": current,
        "longest_streak_days": longest,
        "sessions_this_week": sessions_this_week,
        "days_played": len(dates),
    }


@app.get("/api/leaderboard")
def leaderboard():
    """Rank players — whoever's footage a session belongs to, via the
    `player` meta field — by latest skill estimate, aggregated across all
    of their completed sessions. Sessions with no `player` set (everything
    that predates this field) fall under DEFAULT_PLAYER_NAME, so a
    single-user history still shows up as one row."""
    by_player: dict[str, list[dict]] = {}
    if SESSIONS.exists():
        for d in sorted(SESSIONS.iterdir(), key=lambda p: p.stat().st_mtime):
            if not d.is_dir() or d == TRASH:
                continue
            if get_status(d).get("stage") != "done":
                continue
            meta = _load_json(d, "meta.json")
            name = (meta.get("player") or "").strip() or DEFAULT_PLAYER_NAME
            by_player.setdefault(name, []).append(_compare_summary(d))

    rows = []
    for name, sessions in by_player.items():
        bands = [s["dupr_band"] for s in sessions if s["dupr_band"] is not None]
        won = sum(s["points_won"] or 0 for s in sessions)
        lost = sum(s["points_lost"] or 0 for s in sessions)
        rows.append({
            "player": name,
            "sessions": len(sessions),
            "latest_band": bands[-1] if bands else None,
            "best_band": max(bands) if bands else None,
            "win_pct": round(100.0 * won / (won + lost), 1) if (won + lost) else None,
            "total_distance_ft": round(sum(s["distance_ft"] or 0 for s in sessions), 1),
        })
    rows.sort(key=lambda r: (r["latest_band"] is None, -(r["latest_band"] or 0)))
    return {"players": rows}


@app.get("/api/session/{sid}/synergy")
def synergy(sid: str):
    """Doubles partner positioning report. Computed on demand from
    tracks.parquet + calibration.json (both survive recalibration) rather
    than stored at analyze time, since it's cheap and only needed when a
    user opens it."""
    sdir = _sdir(sid)
    if get_status(sdir).get("stage") != "done":
        raise HTTPException(409, "session not ready")
    m = _load_json(sdir, "metrics.json")
    if m.get("match_type") != "doubles" or m.get("partner_track_id") is None:
        return {"available": False, "reason": "singles session — no partner to compare"}
    tracks_f, calib_f = sdir / "tracks.parquet", sdir / "calibration.json"
    if not tracks_f.exists() or not calib_f.exists():
        return {"available": False, "reason": "tracking data not available"}

    import pandas as pd

    from court import CourtCalibration
    from feedback import synergy_tip
    from metrics import synergy_report
    from tracking import subject_court_positions

    tracks = pd.read_parquet(tracks_f)
    calib_data = load_json(calib_f)
    if not calib_data.get("corners_px"):
        return {"available": False, "reason": "calibration data unreadable"}
    calib = CourtCalibration(calib_data["corners_px"], calib_data.get("kitchen_px"))
    partner_pos = subject_court_positions(tracks, m["partner_track_id"], calib, pipeline.FPS)
    pos_f = sdir / "positions.parquet"
    subject_pos = pd.read_parquet(pos_f) if pos_f.exists() else pd.DataFrame(columns=["t", "x", "y"])
    report = synergy_report(subject_pos, partner_pos)
    report["tip"] = synergy_tip(report)
    return report


_ABSENT = object()


@app.get("/api/session/{sid}")
def session(sid: str):
    sdir = _sdir(sid)
    resp = {"session_id": sid, **get_status(sdir)}
    if resp.get("stage") == "queued":
        resp["queue_position"] = jobs.queue_position(sdir)
    meta_f = sdir / "meta.json"
    resp["meta"] = load_json(meta_f)
    for key, fname in (("metrics", "metrics.json"), ("events", "events.json"),
                       ("shots", "shots.json"), ("points", "points.json"),
                       ("dupr", "dupr.json")):
        data = load_json(sdir / fname, default=_ABSENT)
        if data is not _ABSENT:  # missing or unreadable: omit, same as "stage hasn't run"
            resp[key] = data
    return resp


@app.get("/api/session/{sid}/clip/{n}")
def clip(sid: str, n: int):
    f = _sdir(sid) / "clips" / f"rally_{n:02d}.mp4"
    if not f.exists():
        raise HTTPException(404, "clip not found")
    return FileResponse(f, media_type="video/mp4")


class ClipNote(BaseModel):
    note: str = Field(default="", max_length=500)


@app.patch("/api/session/{sid}/clip/{n}/note")
def set_clip_note(sid: str, n: int, patch: ClipNote):
    """Timestamped note on one rally clip, distinct from the session-level
    `notes` field — for 'watch what happened at this specific rally'."""
    sdir = _sdir(sid)
    meta_f = sdir / "meta.json"
    meta = load_json(meta_f)
    notes = meta.get("clip_notes") or {}
    if patch.note.strip():
        notes[str(n)] = patch.note.strip()
    else:
        notes.pop(str(n), None)
    meta["clip_notes"] = notes
    atomic_write_json(meta_f, meta)
    return {"clip_notes": notes}


@app.get("/api/session/{sid}/highlights.mp4")
def highlights(sid: str):
    """Auto-generated reel of the top rally clips (longest rallies first),
    built on first request and cached until the session is recalibrated."""
    sdir = _sdir(sid)
    if get_status(sdir).get("stage") != "done":
        raise HTTPException(409, "session not ready for a highlight reel")
    out = sdir / "highlights.mp4"
    if not out.exists():
        if media.build_highlights(sdir) is None:
            raise HTTPException(404, "no rally clips available for a highlight reel")
    return FileResponse(out, media_type="video/mp4",
                        filename=f"dinkiq_highlights_{sid}.mp4")


@app.get("/api/session/{sid}/report.pdf")
def report_pdf(sid: str):
    from report import build_report_pdf

    sdir = _sdir(sid)
    if get_status(sdir).get("stage") != "done":
        raise HTTPException(409, "session not ready for a report")

    meta = _load_json(sdir, "meta.json")
    pdf = build_report_pdf(meta, _load_json(sdir, "metrics.json"),
                           _load_json(sdir, "events.json"),
                           _load_json(sdir, "shots.json"),
                           _load_json(sdir, "points.json"),
                           _load_json(sdir, "dupr.json"))
    return Response(content=pdf, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="dinkiq_{sid}.pdf"'})


@app.get("/api/session/{sid}/card.png")
def stat_card(sid: str):
    from card import render_stat_card

    sdir = _sdir(sid)
    if get_status(sdir).get("stage") != "done":
        raise HTTPException(409, "session not ready for a stat card")

    png = render_stat_card(_load_json(sdir, "meta.json"), _load_json(sdir, "metrics.json"),
                           _load_json(sdir, "events.json"), _load_json(sdir, "points.json"),
                           _load_json(sdir, "dupr.json"))
    return Response(content=png, media_type="image/png", headers={
        "Content-Disposition": f'attachment; filename="dinkiq_card_{sid}.png"'})


@app.get("/api/session/{sid}/frame")
def frame(sid: str):
    f = _sdir(sid) / "frame0.jpg"
    if not f.exists():
        raise HTTPException(404, "frame not ready")
    return FileResponse(f)


@app.get("/api/session/{sid}/subject-marker")
def subject_marker(sid: str):
    """Earliest-frame pixel-space bbox for the subject/opponent track, so the
    UI can show a persistent "who are we tracking" overlay on frame0.jpg.
    Returns 200 with nulls (not 404) when tracking data isn't available yet —
    a 404 here would trip the frontend's blanket demo-mode fallback."""
    sdir = _sdir(sid)
    m = _load_json(sdir, "metrics.json")
    tp = sdir / "tracks.parquet"
    empty = {"subject": None, "partner": None, "opponent": None, "opponents": []}
    if not tp.exists() or not m:
        return empty

    import pandas as pd
    tracks = pd.read_parquet(tp)

    def _box(tid):
        if tid is None:
            return None
        rows = tracks[tracks["track_id"] == tid]
        if rows.empty:
            return None
        row = rows.loc[rows["frame"].idxmin()]
        return {"frame": int(row["frame"]), "x1": float(row["x1"]), "y1": float(row["y1"]),
                "x2": float(row["x2"]), "y2": float(row["y2"])}

    opponent_ids = m.get("opponent_track_ids") or (
        [m["opponent_track_id"]] if m.get("opponent_track_id") is not None else [])
    return {"subject": _box(m.get("subject_track_id")),
            "partner": _box(m.get("partner_track_id")),
            "opponent": _box(m.get("opponent_track_id")),  # back-compat single box
            "opponents": [_box(tid) for tid in opponent_ids]}


def _calibratable(status: dict) -> bool:
    """Calibration allowed once ingest finished — including re-calibration of
    completed or failed sessions. Compare fields, not whole dicts (status also
    carries progress/overall/eta keys)."""
    stage, state = status.get("stage"), status.get("state")
    if stage == "ingest":
        return state == "done"
    return state in ("done", "error")


LAST_CALIBRATION_PATH = SESSIONS.parent / "last_calibration.json"


@app.post("/api/session/{sid}/calibrate")
def calibrate(sid: str, cal: Calibration):
    sdir = _sdir(sid)
    if not _calibratable(get_status(sdir)):
        raise HTTPException(409, "session not ready for calibration")
    meta = _load_json(sdir, "meta.json")
    if meta.get("match_type") == "doubles" and cal.partner_px is None:
        raise HTTPException(400, "doubles sessions require partner_px")
    pipeline.clear_derived(sdir)  # keep tracks/ball parquets: pixel-space, reusable
    atomic_write_json(sdir / "calibration.json", cal.model_dump())
    # court corners/kitchen corners are worth reusing for a fixed camera setup —
    # self/partner clicks are NOT saved, those need a fresh click every session
    frame_f = sdir / "frame0.jpg"
    frame_size = None
    if frame_f.exists():
        info = media.probe_video(frame_f)  # ffprobe reads a still frame fine too
        if info.get("width") and info.get("height"):
            frame_size = [info["width"], info["height"]]
    atomic_write_json(LAST_CALIBRATION_PATH, {
        "corners_px": cal.corners_px, "kitchen_px": cal.kitchen_px,
        "frame_size": frame_size,
    })
    jobs.enqueue_analyze(sdir)
    return {"ok": True}


@app.get("/api/last-calibration")
def last_calibration():
    if not LAST_CALIBRATION_PATH.exists():
        return {"available": False}
    data = load_json(LAST_CALIBRATION_PATH)
    if not data.get("corners_px"):  # unreadable/empty file: behave as "never saved"
        return {"available": False}
    return {"available": True, **data}


@app.post("/api/session/{sid}/reprocess")
def reprocess(sid: str):
    """Rerun analysis with the existing calibration (e.g. after model updates)."""
    sdir = _sdir(sid)
    if not (sdir / "calibration.json").exists():
        raise HTTPException(409, "session has no calibration yet")
    if get_status(sdir).get("state") not in ("done", "error"):
        raise HTTPException(409, "session is still processing")
    pipeline.clear_derived(sdir)
    jobs.enqueue_analyze(sdir)
    return {"ok": True}


if __name__ == "__main__":
    if _DINKIQ_PASSWORD_BYTES and not _PROXY_SECRET_BYTES:
        print("warning: DINKIQ_PASSWORD is set but DINKIQ_PROXY_SECRET is not -- "
              "requests through the reverse proxy will be asked for the password again")
    logsetup.configure_logging()
    uvicorn.run(app, host=HOST, port=8100,
                log_config=logsetup.uvicorn_log_config() or uvicorn.config.LOGGING_CONFIG)
