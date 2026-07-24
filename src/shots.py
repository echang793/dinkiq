"""M3 shot classification + serve/third-shot metrics.

Monocular constraints are respected: shot type comes from ball screen-speed
around the subject's hit plus subject court position; serve depth comes from
bounce points (the one moment homography is valid for the ball).

All ball-derived metrics gate on ball coverage — below MIN_COVERAGE the
session reports pose/audio metrics only.
"""

import numpy as np
import pandas as pd

from court import (BOUNCE_LOOKUP_WINDOW_S, COURT_L, COURT_W, NET_Y, court_px_width,
                   dist_from_net, on_court)

MIN_COVERAGE = 0.15    # below this, ball track too sparse to classify anything
HIT_WINDOW_S = 0.30    # ball samples considered "around" a hit
# screen-speed thresholds as fraction of court pixel width per second
DRIVE_SPEED = 1.8
DINK_SPEED = 0.7
FT_S_PER_MPH = 1.46667  # 5280/3600
# how far past the court boundary a bounce can plausibly be and still be a
# real shot landing (as opposed to a glare/reflection/spectator misdetection
# the classical ball detector locked onto for one frame). Much wider than
# points.py's OUT_MARGIN=0.5, which calls a REAL close out — this just
# rejects bounces so far off-court they can't be a real shot at all, before
# depth/placement math ever sees them (same reasoning as MAX_PLAUSIBLE_MPH)
PLAUSIBLE_BOUNCE_MARGIN = 15.0
# fastest verified competitive paddle-sport shots sit well under this; a
# classical motion-blob ball detector occasionally locks onto a false
# positive for one frame, producing a huge apparent px/frame jump — treat
# anything above as a tracking glitch rather than a real shot speed (same
# reasoning as metrics.py's MAX_SPEED_FT_S position-glitch clip)
MAX_PLAUSIBLE_MPH = 75.0


def mph_from_norm(speed_norm: float | None) -> float | None:
    """Normalized ball speed (court-widths/s) -> real mph, using the court's
    known 20 ft width as the pixel-to-feet scale. None if implausible (see
    MAX_PLAUSIBLE_MPH) — a tracking-glitch speed is worse than no number."""
    if speed_norm is None:
        return None
    mph = round(speed_norm * COURT_W / FT_S_PER_MPH, 1)
    return mph if mph <= MAX_PLAUSIBLE_MPH else None


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


def swing_speed_at(ws: pd.DataFrame, t: float) -> float | None:
    """Subject's paddle-swing speed (mph) at hit time t, from wrist keypoint
    velocity. `ws` (events.wrist_speed) is already normalized to court-
    widths/s, so this is just a windowed-median lookup (same pattern as
    subject_pos_at) + the same mph conversion ball_speed_at uses -- putting
    swing speed on the same scale as ball speed instead of only using it to
    detect that a swing happened at all."""
    w = ws[(ws["t"] >= t - 0.15) & (ws["t"] <= t + 0.15)]
    speeds = w["speed"].dropna()
    return mph_from_norm(float(speeds.median())) if len(speeds) else None


def serve_side(x: float) -> str:
    """Left / middle (T) / right third of the court width, receiver's view."""
    third = COURT_W / 3.0
    if x < third:
        return "left"
    if x > 2 * third:
        return "right"
    return "middle"


def _first_plausible_bounce(candidates: pd.DataFrame) -> pd.Series | None:
    """Earliest bounce in a window that's at least within plausible range of
    the court — filters out single-frame glare/reflection/spectator
    misdetections before they're reported as a real shot landing. Wider
    than the strict in/out margin points.py uses, since a bounce can be a
    real (if generously-called) out without being a detection glitch."""
    if not len(candidates):
        return None
    ok = candidates[[on_court(x, y, margin=PLAUSIBLE_BOUNCE_MARGIN)
                     for x, y in zip(candidates["x"], candidates["y"])]]
    if not len(ok):
        return None
    return ok.sort_values("t").iloc[0]


def serve_metrics(rallies: list[dict], subject_hits: np.ndarray,
                  hit_times: np.ndarray, bounces_court: pd.DataFrame) -> dict:
    """Serve depth + placement from the first bounce after a subject serve.

    bounces_court: bounce points already projected to court feet (x, y, t).
    Depth = how close the serve lands to the receiver's baseline (0 or 44).
    Placement = which third of the court width it landed in (left/middle/right).
    """
    serves = []
    for ri, r in enumerate(rallies):
        idx = int(np.argmin(np.abs(hit_times - r["start"])))
        if not subject_hits[idx]:
            continue  # not the subject's serve
        b = bounces_court[(bounces_court["t"] > r["start"]) &
                          (bounces_court["t"] < r["start"] + BOUNCE_LOOKUP_WINDOW_S)]
        first = _first_plausible_bounce(b)
        if first is None:
            continue
        # depth relative to whichever baseline the serve travels toward
        baseline_y = 0.0 if first["y"] < NET_Y else COURT_L
        depth = round(float(abs(baseline_y - first["y"])), 1)
        serves.append({"t": round(float(r["start"]), 2), "depth_ft": depth,
                       "x": round(float(first["x"]), 1), "y": round(float(first["y"]), 1),
                       "side": serve_side(float(first["x"])),
                       # position in `rallies` -- lets points.py join a serve
                       # back to that rally's winner (rallies/outcomes share
                       # the same index order, see pipeline.analyze)
                       "rally_index": ri})
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


def opponent_shot_landings(hit_times: np.ndarray, hitters: list[str],
                           bounces_court: pd.DataFrame, label: str = "opponent1") -> list[dict]:
    """Where a given opponent's shots landed, from the first plausible
    tracked bounce after each of their hits — same lookup pattern as
    serve_metrics, just keyed off the general hitter attribution instead of
    rally starts."""
    landings = []
    for i, h in enumerate(hitters):
        if h != label:
            continue
        t0 = float(hit_times[i])
        b = bounces_court[(bounces_court["t"] > t0) & (bounces_court["t"] < t0 + BOUNCE_LOOKUP_WINDOW_S)]
        first = _first_plausible_bounce(b)
        if first is None:
            continue
        landings.append({"t": round(t0, 2), "x": round(float(first["x"]), 1),
                         "y": round(float(first["y"]), 1), "side": serve_side(float(first["x"]))})
    return landings


def opponent_shot_report(hit_times: np.ndarray, hitters: list[str],
                         bounces_court: pd.DataFrame) -> dict:
    """Landing tendency for every opponent seen in `hitters` (opponent1 in
    singles; opponent1 AND opponent2 in doubles — both counted toward one
    combined report, since the app tracks a single opponent name per
    session rather than per-doubles-player)."""
    labels = sorted({h for h in hitters if h.startswith("opponent")})
    landings = []
    for label in labels:
        landings += opponent_shot_landings(hit_times, hitters, bounces_court, label)
    if not landings:
        return {"shots_tracked": 0}
    sides: dict[str, int] = {}
    for ld in landings:
        sides[ld["side"]] = sides.get(ld["side"], 0) + 1
    dominant = max(sides, key=sides.get)
    return {
        "shots_tracked": len(landings),
        "side_counts": sides,
        "dominant_side": dominant,
        "dominant_side_pct": round(100.0 * sides[dominant] / len(landings), 1),
        "landings": landings,
    }


def shot_report(hit_times: np.ndarray, subject_hits: np.ndarray, ball: pd.DataFrame,
                ball_stats: dict, pos: pd.DataFrame, rallies: list[dict],
                corners_px: list[list[float]], bounces_court: pd.DataFrame,
                fps: float, hitters: list[str] | None = None,
                ws: pd.DataFrame | None = None) -> dict:
    if ball_stats.get("coverage", 0.0) < MIN_COVERAGE:
        return {"available": False,
                "reason": f"ball track coverage {ball_stats.get('coverage', 0):.0%} "
                          f"below {MIN_COVERAGE:.0%} — pose/audio metrics only",
                "ball": ball_stats}

    width_px = court_px_width(corners_px)
    shots = []
    for t, mine in zip(hit_times, subject_hits):
        if not mine:
            continue
        sp = ball_speed_at(ball, float(t), fps)
        norm = sp / width_px if sp is not None else None
        p = subject_pos_at(pos, float(t))
        shot = {"t": round(float(t), 2),
               "type": classify_shot(norm, subject_net_dist_at(pos, float(t))),
               "mph": mph_from_norm(norm)}
        if ws is not None:
            shot["swing_mph"] = swing_speed_at(ws, float(t))
        if p is not None:
            shot["x"], shot["y"] = round(p[0], 1), round(p[1], 1)
        shots.append(shot)
    mix: dict[str, int] = {}
    for s in shots:
        mix[s["type"]] = mix.get(s["type"], 0) + 1
    timed = [s for s in shots if s["mph"] is not None]
    speeds = [s["mph"] for s in timed]
    fastest = max(timed, key=lambda s: s["mph"]) if timed else None
    swing_speeds = [s["swing_mph"] for s in shots if s.get("swing_mph") is not None]
    speed_stats = {
        "avg_shot_mph": round(float(np.mean(speeds)), 1) if speeds else None,
        "top_shot_mph": fastest["mph"] if fastest else None,
        # lets the UI jump straight to this shot's moment in its rally clip
        "top_shot_t": fastest["t"] if fastest else None,
        # paddle-swing speed, distinct from ball speed -- lets a player see
        # "am I not swinging fast enough" vs. "swing is fine, contact isn't"
        "avg_swing_mph": round(float(np.mean(swing_speeds)), 1) if swing_speeds else None,
    }
    report = {"available": True, "ball": ball_stats, "shots": shots, "shot_mix": mix,
             **speed_stats,
             **serve_metrics(rallies, subject_hits, hit_times, bounces_court)}
    if hitters is not None:
        report["opponent_shots"] = opponent_shot_report(hit_times, hitters, bounces_court)
    return report
