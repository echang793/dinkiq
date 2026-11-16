"""Uploads: resumable chunked upload, finish -> new session or replacement video."""

import asyncio
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

import pipeline
import media
import session_status
from api.common import MIN_FREE_BYTES, UPLOADS, free_bytes, get_session_dir, refuse_if_busy
from jsonio import atomic_write_json, load_json
from pipeline import SESSIONS

router = APIRouter()


ALLOWED_EXT = {".mp4", ".mov", ".m4v"}


MAX_UPLOAD_BYTES = 2 << 30  # 2 GB


WARN_DURATION_S = 30 * 60


MATCH_TYPES = {"singles", "doubles"}


UPLOAD_TTL_S = 24 * 3600


MAX_PARTIAL_UPLOADS = 6           # open resumable uploads at once


STALE_PARTIAL_S = 3600            # an idle partial this old may be reclaimed to make room


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


def sweep_uploads() -> None:
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


@router.post("/api/upload/init")
def upload_init(req: UploadInit):
    ext = Path(req.filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"unsupported file type {ext}")
    if req.session_id is not None:  # replacing a video: fail now, not after 2 GB
        refuse_if_busy(get_session_dir(req.session_id))
    if req.size is not None and req.size > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "upload exceeds 2 GB limit")
    free = free_bytes()
    if free - (req.size or 0) < MIN_FREE_BYTES:
        raise HTTPException(507, f"not enough disk space on the server ({free / (1 << 30):.1f} GB free)")
    sweep_uploads()
    _reserve_partial_slot()
    UPLOADS.mkdir(parents=True, exist_ok=True)
    upload_id = uuid.uuid4().hex
    _upload_part(upload_id).touch()
    return {"upload_id": upload_id, "offset": 0}


@router.get("/api/upload/{upload_id}")
def upload_status(upload_id: str):
    """Bytes already stored — the client resumes from here after a drop."""
    part = _upload_part(upload_id)
    if not part.exists():
        raise HTTPException(404, "unknown upload")
    return {"upload_id": upload_id, "offset": part.stat().st_size}


@router.put("/api/upload/{upload_id}")
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
        if free_bytes() < MIN_FREE_BYTES:
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


@router.post("/api/upload/{upload_id}/finish")
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
        sdir = get_session_dir(req.session_id)
        refuse_if_busy(sdir)  # may have started processing since init
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
