"""DinkIQ — FastAPI server: upload, calibrate, process, results, SPA."""

import base64
import json
import os
import re
import secrets
import shutil
import threading
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, PlainTextResponse,
                               Response)
from pydantic import BaseModel, Field, field_validator

import pipeline
from pipeline import SESSIONS, get_status, session_dir

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

app = FastAPI(title="DinkIQ")

# ── Optional HTTP Basic Auth for remote access (e.g. via Tailscale Funnel) ──
# Set DINKIQ_PASSWORD=somepassword (in .env, or the environment directly).
# Localhost bypasses auth regardless — this mirrors vantage_ufc/src/
# pwa_server.py's pattern. Checked via the Host header (not client IP), since
# a Funnel-proxied request still arrives locally but carries the public
# hostname in Host.
_DINKIQ_PASSWORD = os.environ.get("DINKIQ_PASSWORD", "").strip() or None
_DINKIQ_PASSWORD_BYTES = _DINKIQ_PASSWORD.encode() if _DINKIQ_PASSWORD else None


@app.middleware("http")
async def _basic_auth(request, call_next):
    if _DINKIQ_PASSWORD_BYTES is None:
        return await call_next(request)
    host = (request.headers.get("host") or "").split(":")[0].lower()
    if host in ("127.0.0.1", "localhost", ""):
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


class Calibration(BaseModel):
    corners_px: list[list[float]] = Field(min_length=4, max_length=4)
    kitchen_px: list[list[float]] | None = Field(default=None, min_length=4, max_length=4)
    self_px: list[float] = Field(min_length=2, max_length=2)
    partner_px: list[float] | None = Field(default=None, min_length=2, max_length=2)


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


MAX_UPLOAD_BYTES = 2 << 30  # 2 GB
WARN_DURATION_S = 30 * 60


async def _save_upload(sdir: Path, file: UploadFile) -> Path:
    """Validate extension + size, stream file into sdir/raw{ext}. Raises
    HTTPException on bad ext / oversize; cleans up its own partial write but
    leaves sdir itself for the caller to manage."""
    ext = Path(file.filename or "clip.mp4").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"unsupported file type {ext}")
    raw = sdir / f"raw{ext}"
    written = 0
    with raw.open("wb") as f:
        while chunk := await file.read(1 << 20):
            written += len(chunk)
            if written > MAX_UPLOAD_BYTES:
                f.close()
                raw.unlink(missing_ok=True)
                raise HTTPException(413, "upload exceeds 2 GB limit")
            f.write(chunk)
    return raw


MATCH_TYPES = {"singles", "doubles"}

# ── Resumable upload ──────────────────────────────────────────────────────
# A phone on cellular pushing a multi-GB clip through a single POST loses
# the entire transfer on one dropped connection. These three endpoints let
# the client append in chunks and, after a drop, ask where to pick up.
UPLOADS = SESSIONS.parent / "uploads"
UPLOAD_TTL_S = 24 * 3600


def _upload_part(upload_id: str) -> Path:
    """Partial-upload path for an id, rejecting anything not our own uuid
    hex (this lands in a filesystem path, so never trust the client's)."""
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id or ""):
        raise HTTPException(400, "bad upload id")
    return UPLOADS / f"{upload_id}.part"


def _sweep_uploads() -> None:
    """Drop abandoned partials so a failed 2 GB upload can't sit forever."""
    import time
    if not UPLOADS.exists():
        return
    now = time.time()
    for f in UPLOADS.iterdir():
        if f.is_file() and now - f.stat().st_mtime > UPLOAD_TTL_S:
            f.unlink(missing_ok=True)


class UploadInit(BaseModel):
    filename: str = Field(max_length=260)
    match_type: str = "singles"

    @field_validator("match_type")
    @classmethod
    def _valid_match_type(cls, v):
        if v not in MATCH_TYPES:
            raise ValueError(f"match_type must be one of {sorted(MATCH_TYPES)}")
        return v


@app.post("/api/upload/init")
def upload_init(req: UploadInit):
    ext = Path(req.filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"unsupported file type {ext}")
    _sweep_uploads()
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
    if not part.exists():
        raise HTTPException(404, "unknown upload")
    have = part.stat().st_size
    if offset != have:
        # not an error the client can't recover from — tell it where we are
        raise HTTPException(409, f"offset mismatch: server has {have}")
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

    sid = uuid.uuid4().hex[:12]
    sdir = SESSIONS / sid
    sdir.mkdir(parents=True)
    raw = sdir / f"raw{ext}"
    shutil.move(str(part), str(raw))
    return _start_session(sdir, raw, req.filename, req.match_type)


def _start_session(sdir: Path, raw: Path, filename: str, match_type: str) -> dict:
    """Write meta, kick off ingest — shared by both upload paths."""
    meta = {"filename": filename, "match_type": match_type}
    dur = pipeline.probe_video(raw).get("duration", 0.0)
    if dur > WARN_DURATION_S:
        meta["warning"] = (f"video is {dur/60:.0f} min — analysis will take a "
                           "while; consider splitting into games")
    (sdir / "meta.json").write_text(json.dumps(meta))
    threading.Thread(target=pipeline.ingest, args=(sdir, raw), daemon=True).start()
    return {"session_id": sdir.name, "duration_s": dur, "warning": meta.get("warning")}


@app.post("/api/upload")
async def upload(file: UploadFile, match_type: str = Form("singles")):
    if match_type not in MATCH_TYPES:
        raise HTTPException(400, f"match_type must be one of {sorted(MATCH_TYPES)}")
    sid = uuid.uuid4().hex[:12]
    sdir = SESSIONS / sid
    sdir.mkdir(parents=True)
    try:
        raw = await _save_upload(sdir, file)
    except HTTPException:
        shutil.rmtree(sdir)
        raise
    return _start_session(sdir, raw, file.filename, match_type)


@app.post("/api/session/{sid}/reupload")
async def reupload(sid: str, file: UploadFile):
    """Replace a session's source video without changing its session id —
    for when the first upload was wrong or corrupt. Clears all derived
    analysis artifacts and re-runs ingest."""
    sdir = _sdir(sid)
    for old in sdir.glob("raw.*"):
        old.unlink(missing_ok=True)
    raw = await _save_upload(sdir, file)
    pipeline.clear_derived(sdir)
    (sdir / "calibration.json").unlink(missing_ok=True)
    for name in ("video.mp4", "audio.wav", "frame0.jpg", "tracks.parquet",
                "ball.parquet", "ingest.json"):
        (sdir / name).unlink(missing_ok=True)
    meta_f = sdir / "meta.json"
    meta = json.loads(meta_f.read_text()) if meta_f.exists() else {}
    meta["filename"] = file.filename
    meta.pop("warning", None)
    dur = pipeline.probe_video(raw).get("duration", 0.0)
    if dur > WARN_DURATION_S:
        meta["warning"] = (f"video is {dur/60:.0f} min — analysis will take a "
                           "while; consider splitting into games")
    meta_f.write_text(json.dumps(meta))
    threading.Thread(target=pipeline.ingest, args=(sdir, raw), daemon=True).start()
    return {"session_id": sid, "duration_s": dur, "warning": meta.get("warning")}


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
            meta = json.loads(meta_f.read_text()) if meta_f.exists() else {}
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
                "uploaded_at": d.stat().st_mtime,
                **status,
            }
            if status.get("stage") == "queued":
                row["queue_position"] = pipeline.queue_position(d)
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
    import datetime
    points = []
    if SESSIONS.exists():
        for d in SESSIONS.iterdir():
            if not d.is_dir() or get_status(d).get("stage") != "done":
                continue
            m_f, e_f, dp_f, meta_f = (d / "metrics.json", d / "events.json",
                                      d / "dupr.json", d / "meta.json")
            if not m_f.exists():
                continue
            m = json.loads(m_f.read_text())
            ev = json.loads(e_f.read_text()) if e_f.exists() else {}
            dp = json.loads(dp_f.read_text()) if dp_f.exists() else {}
            meta = json.loads(meta_f.read_text()) if meta_f.exists() else {}
            date = meta.get("played_at") or datetime.date.fromtimestamp(
                d.stat().st_mtime).isoformat()
            drills = dp.get("drills") or []
            top_drill = drills[0] if drills else {}
            points.append({
                "session_id": d.name,
                "label": meta.get("label") or meta.get("filename"),
                "date": date,
                "kitchen_pct": m["zone_pct"].get("kitchen"),
                "transition_pct": m["zone_pct"].get("transition"),
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
    meta = json.loads(meta_f.read_text()) if meta_f.exists() else {}
    for k, v in patch.model_dump(exclude_none=True).items():
        meta[k] = v
    meta_f.write_text(json.dumps(meta))
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
    data = json.loads(pf.read_text()) if pf.exists() else {}
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
    pf.write_text(json.dumps(new_data))
    return new_data


@app.post("/api/sessions/delete")
def delete_sessions(req: BulkDelete):
    """Soft-delete: move each session into .trash/ (restorable) rather than
    deleting outright. Trash is purged lazily (see _sweep_trash) after
    TRASH_RETENTION_S."""
    import time

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
        meta_f = d / "meta.json"
        meta = json.loads(meta_f.read_text()) if meta_f.exists() else {}
        meta["deleted_at"] = time.time()
        meta_f.write_text(json.dumps(meta))
        TRASH.mkdir(parents=True, exist_ok=True)
        dest = TRASH / sid
        if dest.exists():  # stale leftover from a prior delete of the same id
            shutil.rmtree(dest)
        shutil.move(str(d), str(dest))
        deleted.append(sid)
    return {"deleted": deleted, "errors": errors}


def _sweep_trash() -> None:
    """Purge trashed sessions older than the retention window."""
    import time

    if not TRASH.exists():
        return
    now = time.time()
    for d in TRASH.iterdir():
        if not d.is_dir():
            continue
        meta_f = d / "meta.json"
        meta = json.loads(meta_f.read_text()) if meta_f.exists() else {}
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
        meta = json.loads(meta_f.read_text()) if meta_f.exists() else {}
        meta.pop("deleted_at", None)
        meta_f.write_text(json.dumps(meta))
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
    f = sdir / name
    return json.loads(f.read_text()) if f.exists() else {}


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
    import datetime

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
    calib_data = json.loads(calib_f.read_text())
    calib = CourtCalibration(calib_data["corners_px"], calib_data.get("kitchen_px"))
    partner_pos = subject_court_positions(tracks, m["partner_track_id"], calib, pipeline.FPS)
    pos_f = sdir / "positions.parquet"
    subject_pos = pd.read_parquet(pos_f) if pos_f.exists() else pd.DataFrame(columns=["t", "x", "y"])
    report = synergy_report(subject_pos, partner_pos)
    report["tip"] = synergy_tip(report)
    return report


@app.get("/api/session/{sid}")
def session(sid: str):
    sdir = _sdir(sid)
    resp = {"session_id": sid, **get_status(sdir)}
    if resp.get("stage") == "queued":
        resp["queue_position"] = pipeline.queue_position(sdir)
    meta_f = sdir / "meta.json"
    resp["meta"] = json.loads(meta_f.read_text()) if meta_f.exists() else {}
    for key, fname in (("metrics", "metrics.json"), ("events", "events.json"),
                       ("shots", "shots.json"), ("points", "points.json"),
                       ("dupr", "dupr.json")):
        f = sdir / fname
        if f.exists():
            resp[key] = json.loads(f.read_text())
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
    meta = json.loads(meta_f.read_text()) if meta_f.exists() else {}
    notes = meta.get("clip_notes") or {}
    if patch.note.strip():
        notes[str(n)] = patch.note.strip()
    else:
        notes.pop(str(n), None)
    meta["clip_notes"] = notes
    meta_f.write_text(json.dumps(meta))
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
        if pipeline.build_highlights(sdir) is None:
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
    (sdir / "calibration.json").write_text(cal.model_dump_json())
    # court corners/kitchen corners are worth reusing for a fixed camera setup —
    # self/partner clicks are NOT saved, those need a fresh click every session
    frame_f = sdir / "frame0.jpg"
    frame_size = None
    if frame_f.exists():
        info = pipeline.probe_video(frame_f)  # ffprobe reads a still frame fine too
        if info.get("width") and info.get("height"):
            frame_size = [info["width"], info["height"]]
    LAST_CALIBRATION_PATH.write_text(json.dumps({
        "corners_px": cal.corners_px, "kitchen_px": cal.kitchen_px,
        "frame_size": frame_size,
    }))
    pipeline.enqueue_analyze(sdir)
    return {"ok": True}


@app.get("/api/last-calibration")
def last_calibration():
    if not LAST_CALIBRATION_PATH.exists():
        return {"available": False}
    data = json.loads(LAST_CALIBRATION_PATH.read_text())
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
    pipeline.enqueue_analyze(sdir)
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8100)
