"""M3 shot classification + serve/third-shot metrics.

Monocular constraints are respected: shot type comes from ball screen-speed
around the subject's hit plus subject court position; serve depth comes from
bounce points (the one moment homography is valid for the ball).

All ball-derived metrics gate on ball coverage — below MIN_COVERAGE the
session reports pose/audio metrics only.
"""

import numpy as np
import pandas as pd

from court import COURT_L, COURT_W, NET_Y, dist_from_net

MIN_COVERAGE = 0.15    # below this, ball track too sparse to classify anything
HIT_WINDOW_S = 0.30    # ball samples considered "around" a hit
# screen-speed thresholds as fraction of court pixel width per second
DRIVE_SPEED = 1.8
DINK_SPEED = 0.7


def _court_px_width(corners_px: list[list[float]]) -> float:
    (flx, fly), (frx, fry), (nrx, nry), (nlx, nly) = corners_px
    return (np.hypot(frx - flx, fry - fly) + np.hypot(nrx - nlx, nry - nly)) / 2.0


def ball_speed_at(ball: pd.DataFrame, t: float, fps: float) -> float | None:
    """Median ball screen-speed (px/s) in the window just after time t."""
    f0, f1 = int(t * fps), int((t + HIT_WINDOW_S) * fps)
    w = ball[(ball["frame"] >= f0) & (ball["frame"] <= f1)].sort_values("frame")
    if len(w) < 3:
        return None
    d = np.hypot(np.diff(w["x"]), np.diff(w["y"]))
    dt = np.clip(np.diff(w["frame"]) / fps, 1e-3, None)
    return float(np.median(d / dt))


def classify_shot(speed_norm: float | None, subject_net_dist: float | None) -> str:
    """Coarse type from normalized ball speed + where the subject stood."""
    if speed_norm is None:
        return "unknown"
    if speed_norm >= DRIVE_SPEED:
        return "drive"
    if speed_norm <= DINK_SPEED:
        # slow ball from the kitchen = dink; slow from deep = drop/reset
        if subject_net_dist is not None and subject_net_dist > 12.0:
            return "drop"
        return "dink"
    return "medium"


def subject_net_dist_at(pos: pd.DataFrame, t: float) -> float | None:
    w = pos[(pos["t"] >= t - 0.4) & (pos["t"] <= t + 0.4)]
    return float(dist_from_net(w["y"].median())) if len(w) else None


def subject_pos_at(pos: pd.DataFrame, t: float) -> tuple[float, float] | None:
    """Subject's (x, y) court-feet position at hit time, for heatmap plotting."""
    w = pos[(pos["t"] >= t - 0.4) & (pos["t"] <= t + 0.4)]
    return (float(w["x"].median()), float(w["y"].median())) if len(w) else None


def serve_side(x: float) -> str:
    """Left / middle (T) / right third of the court width, receiver's view."""
    third = COURT_W / 3.0
    if x < third:
        return "left"
    if x > 2 * third:
        return "right"
    return "middle"


def serve_metrics(rallies: list[dict], subject_hits: np.ndarray,
                  hit_times: np.ndarray, bounces_court: pd.DataFrame) -> dict:
    """Serve depth + placement from the first bounce after a subject serve.

    bounces_court: bounce points already projected to court feet (x, y, t).
    Depth = how close the serve lands to the receiver's baseline (0 or 44).
    Placement = which third of the court width it landed in (left/middle/right).
    """
    serves = []
    for r in rallies:
        idx = int(np.argmin(np.abs(hit_times - r["start"])))
        if not subject_hits[idx]:
            continue  # not the subject's serve
        b = bounces_court[(bounces_court["t"] > r["start"]) &
                          (bounces_court["t"] < r["start"] + 2.5)]
        if not len(b):
            continue
        first = b.sort_values("t").iloc[0]
        # depth relative to whichever baseline the serve travels toward
        baseline_y = 0.0 if first["y"] < NET_Y else COURT_L
        depth = round(float(abs(baseline_y - first["y"])), 1)
        serves.append({"t": round(float(r["start"]), 2), "depth_ft": depth,
                       "x": round(float(first["x"]), 1), "y": round(float(first["y"]), 1),
                       "side": serve_side(float(first["x"]))})
    if not serves:
        return {"serves_measured": 0}
    depths = [s["depth_ft"] for s in serves]
    sides: dict[str, int] = {}
    for s in serves:
        sides[s["side"]] = sides.get(s["side"], 0) + 1
    return {
        "serves_measured": len(serves),
        "avg_serve_depth_from_baseline_ft": round(float(np.mean(depths)), 1),
        "deep_serve_pct": round(100.0 * float(np.mean([d <= 8.0 for d in depths])), 1),
        "serves": serves,
        "serve_side_counts": sides,
    }


def shot_report(hit_times: np.ndarray, subject_hits: np.ndarray, ball: pd.DataFrame,
                ball_stats: dict, pos: pd.DataFrame, rallies: list[dict],
                corners_px: list[list[float]], bounces_court: pd.DataFrame,
                fps: float) -> dict:
    if ball_stats.get("coverage", 0.0) < MIN_COVERAGE:
        return {"available": False,
                "reason": f"ball track coverage {ball_stats.get('coverage', 0):.0%} "
                          f"below {MIN_COVERAGE:.0%} — pose/audio metrics only",
                "ball": ball_stats}

    width_px = _court_px_width(corners_px)
    shots = []
    for t, mine in zip(hit_times, subject_hits):
        if not mine:
            continue
        sp = ball_speed_at(ball, float(t), fps)
        norm = sp / width_px if sp is not None else None
        p = subject_pos_at(pos, float(t))
        shot = {"t": round(float(t), 2),
               "type": classify_shot(norm, subject_net_dist_at(pos, float(t)))}
        if p is not None:
            shot["x"], shot["y"] = round(p[0], 1), round(p[1], 1)
        shots.append(shot)
    mix: dict[str, int] = {}
    for s in shots:
        mix[s["type"]] = mix.get(s["type"], 0) + 1
    return {"available": True, "ball": ball_stats, "shots": shots, "shot_mix": mix,
            **serve_metrics(rallies, subject_hits, hit_times, bounces_court)}
