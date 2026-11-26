"""Highlight reel as a background job: start it, poll it, then fetch it.

The reel is a stream-copy concat of the best rally clips, so it is quick -- but a request that
silently waits on ffmpeg gives a phone nothing to show and invites a second tap that starts a
second build. State is kept in memory per session; the file on disk is the source of truth for
"ready", so a recalibration (which deletes it) is reflected without any bookkeeping here.
"""

import logging
import threading
from pathlib import Path

import media

log = logging.getLogger("dinkiq.reel")

_lock = threading.Lock()
_jobs: dict[str, dict] = {}       # session id -> {"state": "building" | "error", "error": str | None}


def reset() -> None:
    """Forget every job (tests)."""
    with _lock:
        _jobs.clear()


def status(sdir: Path) -> dict:
    with _lock:
        job = _jobs.get(sdir.name)
        if job and job["state"] == "building":
            return {"state": "building", "error": None}
    if (sdir / "highlights.mp4").exists():
        return {"state": "ready", "error": None}
    if job:
        return {"state": job["state"], "error": job["error"]}
    return {"state": "none", "error": None}


def _build(sdir: Path) -> None:
    error = None
    try:
        if media.build_highlights(sdir) is None:
            error = "no rally clips available for a highlight reel"
    except Exception as e:
        log.exception("highlight reel failed for %s", sdir.name)
        error = f"couldn't build the reel: {e}"
    with _lock:
        if error:
            _jobs[sdir.name] = {"state": "error", "error": error}
        else:
            _jobs.pop(sdir.name, None)


def start(sdir: Path) -> dict:
    """Begin building unless it is built or already building. Returns the status after the call."""
    with _lock:
        building = _jobs.get(sdir.name, {}).get("state") == "building"
        if not building and not (sdir / "highlights.mp4").exists():
            _jobs[sdir.name] = {"state": "building", "error": None}
            threading.Thread(target=_build, args=(sdir,), daemon=True, name=f"reel-{sdir.name}").start()
    return status(sdir)
