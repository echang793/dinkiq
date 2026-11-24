"""Sessions: list, detail, metadata, point corrections, clips, calibration, reprocess."""

import datetime
import math
import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

import pipeline
import jobs
import media
from api.common import LAST_CALIBRATION_PATH, dir_bytes, get_session_dir, read_artifact, iter_sessions
from jsonio import atomic_write_json, load_json
from session_status import get_status

router = APIRouter()


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


@router.get("/api/sessions")
def sessions(label: str | None = None, opponent: str | None = None,
            date_from: str | None = None, date_to: str | None = None,
            context: str | None = None):
    """Session list, newest first. Optional case-insensitive substring filters
    on label/opponent, an inclusive played_at date range (YYYY-MM-DD), and an
    exact match on context (practice/league/tournament)."""
    out = []
    for d in iter_sessions(newest_first=True):
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
            "disk_bytes": dir_bytes(d),
            "uploaded_at": d.stat().st_mtime,
            **status,
        }
        if status.get("stage") == "queued":
            row["queue_position"] = jobs.queue_position(d)
        if status.get("stage") == "done":
            # summary fields the sessions list/table renders directly —
            # real numbers, not a UI placeholder, for every completed row
            m = read_artifact(d, "metrics.json")
            ev = read_artifact(d, "events.json")
            dp = read_artifact(d, "dupr.json")
            row["kitchen_pct"] = m.get("zone_pct", {}).get("kitchen")
            row["avg_rally_hits"] = ev.get("avg_rally_hits")
            row["dupr_band"] = dp.get("band")
            row["dupr_confidence"] = dp.get("confidence")
        out.append(row)
    return out


@router.patch("/api/session/{sid}/meta")
def patch_meta(sid: str, patch: MetaPatch):
    sdir = get_session_dir(sid)
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


@router.patch("/api/session/{sid}/points/{idx}")
def correct_point(sid: str, idx: int, patch: PointCorrection):
    """Manually override a heuristically-detected rally winner (the
    audio+ball heuristic in points.py can misjudge close calls). Recomputes
    the session's win/loss summary from the corrected outcomes; per-player
    hit counts are independent of who won each rally, so they're carried
    over unchanged rather than recomputed (the hitter attribution used to
    build them isn't persisted anywhere)."""
    from points import point_summary, serve_side_win_rates, shot_type_outcomes

    sdir = get_session_dir(sid)
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
    shots_report = read_artifact(sdir, "shots.json")
    rallies = read_artifact(sdir, "events.json").get("rallies", [])
    summary["serve_side_win_rates"] = serve_side_win_rates(
        shots_report.get("serves") or [], outcomes)
    summary["shot_type_outcomes"] = shot_type_outcomes(
        shots_report.get("shots") or [], rallies, outcomes)
    new_data = {"outcomes": outcomes, **summary}
    atomic_write_json(pf, new_data)
    return new_data


_ABSENT = object()


@router.get("/api/session/{sid}")
def session(sid: str):
    sdir = get_session_dir(sid)
    resp = {"session_id": sid, **get_status(sdir)}
    if resp.get("stage") == "queued":
        resp["queue_position"] = jobs.queue_position(sdir)
    meta_f = sdir / "meta.json"
    resp["meta"] = load_json(meta_f)
    for key, fname in (("metrics", "metrics.json"), ("events", "events.json"),
                       ("shots", "shots.json"), ("points", "points.json"),
                       ("dupr", "dupr.json"), ("hits", "hits.json")):
        data = load_json(sdir / fname, default=_ABSENT)
        if data is not _ABSENT:  # missing or unreadable: omit, same as "stage hasn't run"
            resp[key] = data
    return resp


@router.get("/api/session/{sid}/clip/{n}")
def clip(sid: str, n: int):
    f = get_session_dir(sid) / "clips" / f"rally_{n:02d}.mp4"
    if not f.exists():
        raise HTTPException(404, "clip not found")
    return FileResponse(f, media_type="video/mp4")


class ClipNote(BaseModel):
    note: str = Field(default="", max_length=500)


@router.patch("/api/session/{sid}/clip/{n}/note")
def set_clip_note(sid: str, n: int, patch: ClipNote):
    """Timestamped note on one rally clip, distinct from the session-level
    `notes` field — for 'watch what happened at this specific rally'."""
    sdir = get_session_dir(sid)
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


@router.get("/api/session/{sid}/frame")
def frame(sid: str):
    f = get_session_dir(sid) / "frame0.jpg"
    if not f.exists():
        raise HTTPException(404, "frame not ready")
    return FileResponse(f)


@router.get("/api/session/{sid}/subject-marker")
def subject_marker(sid: str):
    """Earliest-frame pixel-space bbox for the subject/opponent track, so the
    UI can show a persistent "who are we tracking" overlay on frame0.jpg.
    Returns 200 with nulls (not 404) when tracking data isn't available yet —
    a 404 here would trip the frontend's blanket demo-mode fallback."""
    sdir = get_session_dir(sid)
    m = read_artifact(sdir, "metrics.json")
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


@router.post("/api/session/{sid}/calibrate")
def calibrate(sid: str, cal: Calibration):
    sdir = get_session_dir(sid)
    if not _calibratable(get_status(sdir)):
        raise HTTPException(409, "session not ready for calibration")
    meta = read_artifact(sdir, "meta.json")
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


@router.get("/api/last-calibration")
def last_calibration():
    if not LAST_CALIBRATION_PATH.exists():
        return {"available": False}
    data = load_json(LAST_CALIBRATION_PATH)
    if not data.get("corners_px"):  # unreadable/empty file: behave as "never saved"
        return {"available": False}
    return {"available": True, **data}


@router.post("/api/session/{sid}/reprocess")
def reprocess(sid: str):
    """Rerun analysis with the existing calibration (e.g. after model updates)."""
    sdir = get_session_dir(sid)
    if not (sdir / "calibration.json").exists():
        raise HTTPException(409, "session has no calibration yet")
    if get_status(sdir).get("state") not in ("done", "error"):
        raise HTTPException(409, "session is still processing")
    pipeline.clear_derived(sdir)
    jobs.enqueue_analyze(sdir)
    return {"ok": True}
