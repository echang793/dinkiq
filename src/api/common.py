"""Shared by the API modules: storage paths, session lookup, iteration, summaries."""

import shutil
from collections.abc import Iterator
from pathlib import Path

from fastapi import HTTPException

import jobs
from jsonio import load_json
from pipeline import SESSIONS, session_dir
from session_status import get_status

TRASH = SESSIONS / ".trash"
UPLOADS = SESSIONS.parent / "uploads"
LAST_CALIBRATION_PATH = SESSIONS.parent / "last_calibration.json"
MIN_FREE_BYTES = 2 << 30  # keep this much disk free; below it, refuse uploads
DEFAULT_PLAYER_NAME = "You"


def belongs_to(meta: dict, player: str | None) -> bool:
    """Is this session `player`'s footage? A session with no `player` tag predates the
    field (or never set it) and counts as the user's own; one tagged with a different
    name is someone else's. No player given = no filtering."""
    if not player or not player.strip():
        return True
    tagged = (meta.get("player") or "").strip()
    return not tagged or tagged.casefold() == player.strip().casefold()


def iter_sessions(*, newest_first: bool | None = None, done_only: bool = False) -> Iterator[Path]:
    """Session directories, excluding the trash and any other hidden dir.

    newest_first: None = filesystem order, True = newest mtime first, False =
    oldest first. done_only skips sessions that haven't finished analysis.
    (Seven endpoints each used to carry their own copy of this loop.)"""
    if not SESSIONS.exists():
        return
    dirs = [d for d in SESSIONS.iterdir() if d.is_dir() and not d.name.startswith(".")]
    if newest_first is not None:
        dirs.sort(key=lambda p: p.stat().st_mtime, reverse=newest_first)
    for d in dirs:
        if done_only and get_status(d).get("stage") != "done":
            continue
        yield d


def get_session_dir(sid: str) -> Path:
    try:
        d = session_dir(sid)
    except ValueError:
        raise HTTPException(400, "bad session id")
    if not d.exists():
        raise HTTPException(404, "session not found")
    return d


def read_artifact(sdir: Path, name: str) -> dict:
    return load_json(sdir / name)


def refuse_if_busy(sdir: Path) -> None:
    if jobs.is_busy(sdir):
        raise HTTPException(409, "session is still processing")


def compare_summary(sdir: Path) -> dict:
    m = read_artifact(sdir, "metrics.json")
    ev = read_artifact(sdir, "events.json")
    pts = read_artifact(sdir, "points.json")
    dp = read_artifact(sdir, "dupr.json")
    meta = read_artifact(sdir, "meta.json")
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


def free_bytes() -> int:
    UPLOADS.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(UPLOADS).free


def dir_bytes(d: Path) -> int:
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
