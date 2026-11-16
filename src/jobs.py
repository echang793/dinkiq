"""Job control: the single analysis worker + queue, busy checks, startup recovery.

One analysis at a time (YOLO/MPS contention); the queue lives in memory only,
which is why recover_interrupted() exists.
"""

import logging
import queue
import threading
from pathlib import Path

import media
import stages
from jsonio import load_json
from pipeline import SESSIONS
from session_status import get_status, set_status

log = logging.getLogger("dinkiq.jobs")


# ---- single-worker analysis queue: one YOLO/MPS job at a time ----
_jobs: queue.Queue = queue.Queue()


_worker_started = False


_worker_thread: threading.Thread | None = None


_current: Path | None = None  # session the worker is analysing right now


_worker_lock = threading.Lock()


_queue_order: list[Path] = []  # FIFO of pending/running session dirs, for position lookup


_queue_lock = threading.Lock()


def _worker() -> None:
    global _current
    while True:
        sdir = _jobs.get()
        _current = sdir
        try:
            stages.analyze(sdir)
        except Exception:
            log.exception("analysis worker: unhandled error for %s", sdir.name)
        finally:
            _current = None
            with _queue_lock:
                if sdir in _queue_order:
                    _queue_order.remove(sdir)
            _jobs.task_done()


def enqueue_analyze(sdir: Path) -> None:
    global _worker_started, _worker_thread
    with _worker_lock:
        if not _worker_started:
            _worker_thread = threading.Thread(target=_worker, daemon=True)
            _worker_thread.start()
            _worker_started = True
    set_status(sdir, "queued", "queued")
    with _queue_lock:
        _queue_order.append(sdir)
    _jobs.put(sdir)


def worker_status() -> dict:
    """For /api/health. alive is None until the first job starts the worker
    (it is started lazily), False if it has died."""
    t = _worker_thread
    cur = _current
    with _queue_lock:
        queued = len(_queue_order) - (1 if cur is not None else 0)
    return {"started": t is not None, "alive": t.is_alive() if t is not None else None,
            "queued": max(0, queued), "running": cur.name if cur is not None else None}


def queue_position(sdir: Path) -> int | None:
    """0 = currently running/next up, N = N jobs ahead, None = not queued."""
    with _queue_lock:
        try:
            return _queue_order.index(sdir)
        except ValueError:
            return None


BUSY_STATES = ("running", "queued")


def is_busy(sdir: Path) -> bool:
    """True while analysis/ingest owns the session's files."""
    return get_status(sdir).get("state") in BUSY_STATES


def _spawn_ingest(sdir: Path, raw: Path) -> None:
    threading.Thread(target=media.ingest, args=(sdir, raw), daemon=True).start()


_ANALYSIS_STAGES = ("queued", "tracking", "metrics", "events", "shots", "points", "rating")


def recover_interrupted() -> dict:
    """Repair sessions a server restart left mid-flight.

    The job queue lives only in memory, so a restart (or crash) used to strand
    sessions in queued/tracking/... forever -- and recalibrate and reprocess
    both refuse a session that still *looks* busy, leaving delete as the only
    way out. Call once at startup, before serving:
      - analysis was queued/running and a calibration exists -> re-queue it
        (oldest first, so FIFO order survives the restart)
      - analysis interrupted with no calibration -> error (recalibrate unlocks)
      - ingest interrupted and the raw upload is still there -> restart ingest
      - ingest interrupted, no raw file -> error telling the user to re-upload
    Sessions that are done/errored/idle, trashed, or whose status.json is
    unreadable are left alone.
    """
    out: dict = {"requeued": [], "reingested": [], "failed": []}
    if not SESSIONS.exists():
        return out
    pending: list[tuple[float, Path, str]] = []
    for d in SESSIONS.iterdir():
        if not d.is_dir() or d.name.startswith("."):
            continue
        f = d / "status.json"
        st = load_json(f)  # missing/corrupt -> {} -> no state -> skipped
        stage, state = st.get("stage"), st.get("state")
        if state not in BUSY_STATES:
            continue
        if stage == "ingest":
            raws = sorted(d.glob("raw.*"))
            if raws:
                set_status(d, "ingest", "running")
                _spawn_ingest(d, raws[0])
                out["reingested"].append(d.name)
            else:
                set_status(d, "ingest", "error",
                           "processing was interrupted by a server restart and the upload "
                           "is gone — re-upload the video")
                out["failed"].append(d.name)
        elif stage in _ANALYSIS_STAGES:
            pending.append((f.stat().st_mtime, d, stage))
    for _, d, stage in sorted(pending, key=lambda t: t[0]):
        if (d / "calibration.json").exists():
            enqueue_analyze(d)
            out["requeued"].append(d.name)
        else:
            set_status(d, stage, "error",
                       "analysis was interrupted by a server restart — recalibrate to run it again")
            out["failed"].append(d.name)
    return out
