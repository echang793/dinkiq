"""DinkIQ — FastAPI server: upload, calibrate, process, results, SPA."""

import base64
import json
import os
import secrets
import shutil
import threading
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field, field_validator

import pipeline
from pipeline import SESSIONS, get_status, session_dir

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
ALLOWED_EXT = {".mp4", ".mov", ".m4v"}

app = FastAPI(title="DinkIQ")

# ── Optional HTTP Basic Auth for remote access (e.g. via Tailscale Funnel) ──
# Set DINKIQ_PASSWORD=somepassword. Localhost bypasses auth regardless — this
# mirrors vantage_ufc/src/pwa_server.py's pattern. Checked via the Host header
# (not client IP), since a Funnel-proxied request still arrives locally but
# carries the public hostname in Host.
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
    context: str | None = Field(default=None)

    @field_validator("context")
    @classmethod
    def _valid_context(cls, v):
        if v is not None and v not in SESSION_CONTEXTS:
            raise ValueError(f"context must be one of {sorted(SESSION_CONTEXTS)}")
        return v


@app.get("/")
def index():
    return FileResponse(STATIC / "dashboard.html")


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
    meta = {"filename": file.filename, "match_type": match_type}
    dur = pipeline.probe_video(raw).get("duration", 0.0)
    if dur > WARN_DURATION_S:
        meta["warning"] = (f"video is {dur/60:.0f} min — analysis will take a "
                           "while; consider splitting into games")
    (sdir / "meta.json").write_text(json.dumps(meta))
    threading.Thread(target=pipeline.ingest, args=(sdir, raw), daemon=True).start()
    return {"session_id": sid, "duration_s": dur, "warning": meta.get("warning")}


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
    pipeline.enqueue_analyze(sdir)
    return {"ok": True}


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
    uvicorn.run(app, host="127.0.0.1", port=8100)
