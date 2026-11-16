"""Housekeeping that runs hourly (and at startup): stale uploads, expired trash, old raw files."""

import asyncio
import logging
import os
import shutil
import time

from api.uploads import sweep_uploads
import jobs
from api.common import TRASH
from jsonio import load_json
from pipeline import SESSIONS
from session_status import get_status

log = logging.getLogger("dinkiq.maintenance")


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


def sweep_raw() -> None:
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


TRASH_RETENTION_S = 7 * 24 * 3600


def sweep_trash() -> None:
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


MAINTENANCE_INTERVAL_S = 3600


def maintenance_once() -> None:
    """Housekeeping that used to run only when someone happened to start an
    upload or open the trash view: abandoned partials and expired trash."""
    sweep_uploads()
    sweep_trash()
    sweep_raw()


async def maintenance_loop() -> None:
    while True:
        await asyncio.sleep(MAINTENANCE_INTERVAL_S)
        try:
            await asyncio.to_thread(maintenance_once)
        except Exception:
            log.exception("maintenance pass failed (will retry)")
