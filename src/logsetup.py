"""Rotating, timestamped logging.

The launchd log was 14 MB of 119,000 identical lines with no timestamps, so
nobody could tell a crash loop had started, when, or that it was still going.
Logs now go to a size-capped rotating file (5 MB x 5 files) with a timestamp
on every line.
"""

import logging
import logging.handlers
import os
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 5
LOG_NAME = "dinkiq-app.log"


def default_log_dir() -> Path:
    return Path(os.environ.get("DINKIQ_LOG_DIR") or Path.home() / ".vantage" / "logs")


def _log_path(log_dir: Path | str | None) -> Path | None:
    """The log file path, creating its directory; None if that isn't possible
    (logging problems must never stop the server from starting)."""
    d = Path(log_dir) if log_dir else default_log_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return d / LOG_NAME


def configure_logging(log_dir: Path | str | None = None) -> Path | None:
    """Attach the rotating file handler to the "dinkiq" logger (idempotent)."""
    path = _log_path(log_dir)
    if path is None:
        return None
    lg = logging.getLogger("dinkiq")
    lg.setLevel(logging.INFO)
    already = any(isinstance(h, logging.handlers.RotatingFileHandler)
                  and Path(h.baseFilename) == path for h in lg.handlers)
    if not already:
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        lg.addHandler(handler)
    return path


def uvicorn_log_config(log_dir: Path | str | None = None) -> dict | None:
    """uvicorn dictConfig routing its error + access logs and ours through one
    rotating handler. None if the log dir isn't writable (caller falls back to
    uvicorn's default)."""
    path = _log_path(log_dir)
    if path is None:
        return None
    to_file = {"handlers": ["file"], "level": "INFO", "propagate": False}
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"default": {"format": LOG_FORMAT}},  # dictConfig key is `format`, not `fmt`
        "handlers": {"file": {
            "class": "logging.handlers.RotatingFileHandler", "filename": str(path),
            "maxBytes": MAX_BYTES, "backupCount": BACKUPS, "encoding": "utf-8",
            "formatter": "default"}},
        "loggers": {"uvicorn": dict(to_file), "uvicorn.error": {"level": "INFO"},
                    "uvicorn.access": dict(to_file), "dinkiq": dict(to_file)},
    }
