"""Pickle Coach — FastAPI server: upload, calibrate, process, results, SPA."""

import json
import shutil
import threading
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

import pipeline
from pipeline import SESSIONS, get_status, session_dir

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
ALLOWED_EXT = {".mp4", ".mov", ".m4v"}

app = FastAPI(title="Pickle Coach")


class Calibration(BaseModel):
    corners_px: list[list[float]] = Field(min_length=4, max_length=4)
    kitchen_px: list[list[float]] | None = Field(default=None, min_length=4, max_length=4)
    self_px: list[float] = Field(min_length=2, max_length=2)


class BulkDelete(BaseModel):
    ids: list[str] = Field(min_length=1)


class MetaPatch(BaseModel):
    label: str | None = Field(default=None, max_length=120)
    played_at: str | None = Field(default=None, max_length=10)  # YYYY-MM-DD
    known_dupr: float | None = Field(default=None, ge=2.0, le=8.0)


@app.get("/")
def index():
    return FileResponse(STATIC / "dashboard.html")


MAX_UPLOAD_BYTES = 2 << 30  # 2 GB
WARN_DURATION_S = 30 * 60


@app.post("/api/upload")
async def upload(file: UploadFile):
    ext = Path(file.filename or "clip.mp4").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"unsupported file type {ext}")
    sid = uuid.uuid4().hex[:12]
    sdir = SESSIONS / sid
    sdir.mkdir(parents=True)
    raw = sdir / f"raw{ext}"
    written = 0
    with raw.open("wb") as f:
        while chunk := await file.read(1 << 20):
            written += len(chunk)
            if written > MAX_UPLOAD_BYTES:
                f.close()
                shutil.rmtree(sdir)
                raise HTTPException(413, "upload exceeds 2 GB limit")
            f.write(chunk)
    meta = {"filename": file.filename}
    dur = pipeline.probe_video(raw).get("duration", 0.0)
    if dur > WARN_DURATION_S:
        meta["warning"] = (f"video is {dur/60:.0f} min — analysis will take a "
                           "while; consider splitting into games")
    (sdir / "meta.json").write_text(json.dumps(meta))
    threading.Thread(target=pipeline.ingest, args=(sdir, raw), daemon=True).start()
    return {"session_id": sid, "duration_s": dur, "warning": meta.get("warning")}


@app.get("/api/sessions")
def sessions():
    out = []
    if SESSIONS.exists():
        for d in sorted(SESSIONS.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if d.is_dir():
                meta_f = d / "meta.json"
                meta = json.loads(meta_f.read_text()) if meta_f.exists() else {}
                status = get_status(d)
                row = {
                    "session_id": d.name,
                    "filename": meta.get("filename"),
                    "label": meta.get("label"),
                    "played_at": meta.get("played_at"),
                    "known_dupr": meta.get("known_dupr"),
                    "uploaded_at": d.stat().st_mtime,
                    **status,
                }
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
        shutil.rmtree(d)
        deleted.append(sid)
    return {"deleted": deleted, "errors": errors}


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


NON_METRIC_FIELDS = {"session_id", "label", "played_at"}


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


@app.get("/api/session/{sid}")
def session(sid: str):
    sdir = _sdir(sid)
    resp = {"session_id": sid, **get_status(sdir)}
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
        "Content-Disposition": f'attachment; filename="picklecoach_{sid}.pdf"'})


@app.get("/api/session/{sid}/frame")
def frame(sid: str):
    f = _sdir(sid) / "frame0.jpg"
    if not f.exists():
        raise HTTPException(404, "frame not ready")
    return FileResponse(f)


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
