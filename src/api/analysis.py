"""Deeper looks at one finished session: extra analytics, clip windows, the full video."""

import pandas as pd
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

import analytics
from api.common import get_session_dir, read_artifact
from jsonio import load_json
from session_status import get_status

router = APIRouter()


def _done_dir(sid: str):
    sdir = get_session_dir(sid)
    if get_status(sdir).get("stage") != "done":
        raise HTTPException(409, "session not ready")
    return sdir


@router.get("/api/session/{sid}/analytics")
def session_analytics(sid: str):
    """Win rate by rally length, pace, return-and-run, third-shot profile, error direction and
    where points end -- computed on demand from the stored artifacts, so older sessions get it
    without a reprocess. Each block carries its sample size and `available`."""
    sdir = _done_dir(sid)
    pos_f = sdir / "positions.parquet"
    try:
        positions = pd.read_parquet(pos_f) if pos_f.exists() else None
    except (OSError, ValueError):
        positions = None
    hits = load_json(sdir / "hits.json", default=[])
    return analytics.extras(read_artifact(sdir, "events.json"), hits if isinstance(hits, list) else [],
                            read_artifact(sdir, "points.json"), read_artifact(sdir, "shots.json"),
                            positions)


@router.get("/api/session/{sid}/moments")
def session_moments(sid: str):
    """Seekable windows into the full video behind each headline number (won/lost rallies,
    unforced errors, serves, each shot type, the longest rally)."""
    sdir = _done_dir(sid)
    return analytics.moments(read_artifact(sdir, "events.json"), read_artifact(sdir, "points.json"),
                             read_artifact(sdir, "shots.json"))


@router.get("/api/session/{sid}/video")
def session_video(sid: str):
    """The whole normalized match video. Served with Range support so a player can seek straight
    to a rally instead of downloading the file first."""
    f = get_session_dir(sid) / "video.mp4"
    if not f.exists():
        raise HTTPException(404, "video not found")
    return FileResponse(f, media_type="video/mp4")
