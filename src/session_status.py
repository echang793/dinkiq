"""Per-session status.json: where a session is in ingest -> analysis -> done.

Written atomically (see jsonio), read by the API on every poll, and the signal
that startup recovery and the delete/replace guards act on.
"""

from pathlib import Path

from jsonio import atomic_write_json, load_json

# rough share of analysis wall-clock per stage (tracking dominates; measured
# on the benchmark clip) — used for the overall progress bar + ETA
STAGE_WEIGHTS = {"queued": 0.0, "tracking": 0.80, "metrics": 0.82,
                 "events": 0.90, "shots": 0.97, "points": 0.99,
                 "rating": 1.0, "done": 1.0}


def set_status(sdir: Path, stage: str, state: str, error: str | None = None,
               progress: float | None = None,
               eta_seconds: float | None = None) -> None:
    payload: dict = {"stage": stage, "state": state}
    if error:
        payload["error"] = error
    prev_w = 0.0
    for s, w in STAGE_WEIGHTS.items():
        if s == stage:
            payload["overall"] = round(prev_w + (w - prev_w) * (progress or 0.0), 3) \
                if progress is not None else round(prev_w, 3)
            break
        prev_w = w
    else:
        payload["overall"] = 1.0 if state == "done" and stage == "done" else 0.0
    if progress is not None:
        payload["progress"] = round(progress, 3)
    if eta_seconds is not None:
        payload["eta_seconds"] = int(eta_seconds)
    atomic_write_json(sdir / "status.json", payload)


def get_status(sdir: Path) -> dict:
    # A truncated status.json (crash mid-write, pre-atomic writes) is reported as
    # an error rather than raising: state "error" is what unlocks recalibrate /
    # re-analyze, so the session is recoverable instead of 500ing every list.
    return load_json(
        sdir / "status.json", default={"stage": "new", "state": "idle"},
        on_corrupt={"stage": "error", "state": "error",
                    "error": "status.json is unreadable — recalibrate or re-analyze to recover"})
