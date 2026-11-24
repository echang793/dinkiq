"""M1 skill metrics from the subject's court positions.

Input: DataFrame with columns (frame, t, x, y) in court feet-coords.
Output: dict of positioning/movement metrics + heatmap grid, JSON-serializable.
"""

import numpy as np
import pandas as pd

from court import COURT_L, COURT_W, dist_from_net, zone_for

CELL_FT = 2.0  # heatmap cell size
GRID_W = int(COURT_W / CELL_FT)   # 10
GRID_L = int(COURT_L / CELL_FT)   # 22
MAX_SPEED_FT_S = 30.0  # displacement cap: faster than any human = tracking glitch
MOVEMENT_BUCKET_S = 60.0  # movement-curve window — coarse enough to smooth noise
MIN_BUCKET_SAMPLES = 3    # skip a bucket with too little signal to average honestly
LIVE_PAD_S = 1.0          # include the approach to the first hit and the recovery after the last
MIN_LIVE_SAMPLES = 450    # ~30 s of tracked positions (15 Hz). The sample had 9.8 s across 3 rallies:
                          # that slice moved the rating half a band, so too little to trust over all-time


def _movement_curve(step_t: np.ndarray, speed: np.ndarray, valid: np.ndarray) -> list[dict]:
    """Avg movement speed per MOVEMENT_BUCKET_S window, for a fatigue/momentum
    plot over the course of the session. Buckets with too few valid samples
    are skipped rather than shown as a misleadingly flat/zero value."""
    if not len(step_t):
        return []
    bucket_idx = (step_t // MOVEMENT_BUCKET_S).astype(int)
    curve = []
    for b in range(int(bucket_idx.max()) + 1):
        m = (bucket_idx == b) & valid
        if m.sum() < MIN_BUCKET_SAMPLES:
            continue
        curve.append({"t_start": round(b * MOVEMENT_BUCKET_S, 1),
                      "avg_speed_ft_s": round(float(speed[m].mean()), 2)})
    return curve


def _smooth(series: pd.Series, window: int = 5) -> pd.Series:
    return series.rolling(window, center=True, min_periods=1).median()


SYNERGY_BUCKET_S = 1.0  # coarse enough to tolerate independent stitching gaps


def synergy_report(subject_pos: pd.DataFrame, partner_pos: pd.DataFrame) -> dict:
    """Doubles partner positioning: how far apart the pair plays and how
    much of the court they cover redundantly, from each player's own
    court-projected positions (median-bucketed to a common time grid since
    the two tracks aren't sampled at identical timestamps)."""
    if not len(subject_pos) or not len(partner_pos):
        return {"available": False, "reason": "insufficient tracking for one or both players"}

    def bucketed(df: pd.DataFrame) -> pd.DataFrame:
        b = (df["t"] // SYNERGY_BUCKET_S).astype(int)
        return df.assign(bucket=b).groupby("bucket")[["x", "y"]].median()

    sb, pb = bucketed(subject_pos), bucketed(partner_pos)
    common = sb.index.intersection(pb.index)
    if not len(common):
        return {"available": False, "reason": "no overlapping tracked time between players"}
    sep = np.hypot(sb.loc[common, "x"] - pb.loc[common, "x"],
                   sb.loc[common, "y"] - pb.loc[common, "y"])

    def cells(df: pd.DataFrame) -> set[tuple[int, int]]:
        gx = np.clip((df["x"] / CELL_FT).astype(int), 0, GRID_W - 1)
        gy = np.clip((df["y"] / CELL_FT).astype(int), 0, GRID_L - 1)
        return set(zip(gy.tolist(), gx.tolist()))

    sc, pc = cells(subject_pos), cells(partner_pos)
    union = sc | pc
    return {
        "available": True,
        "samples": int(len(common)),
        "avg_separation_ft": round(float(sep.mean()), 1),
        "min_separation_ft": round(float(sep.min()), 1),
        "coverage_overlap_pct": round(100.0 * len(sc & pc) / len(union), 1) if union else None,
    }


def _live_mask(t: np.ndarray, windows: list[tuple[float, float]] | None) -> np.ndarray:
    m = np.zeros(len(t), dtype=bool)
    for a, b in windows or []:
        m |= (t >= a - LIVE_PAD_S) & (t <= b + LIVE_PAD_S)
    return m


def compute_metrics(pos: pd.DataFrame, fps: float, camera_cuts: int = 0,
                    secondary_court_tracks: int = 0,
                    play_windows: list[tuple[float, float]] | None = None) -> dict:
    """play_windows: (start, end) seconds of each rally. When enough of them is tracked,
    zone occupancy and the movement curve describe LIVE PLAY only. On the sample ~98%
    of the video was not in a rally, so "kitchen discipline" (30% of the rating) mostly
    measured where the subject waited between points. Distance, speed, heatmap and
    coverage stay whole-session."""
    if len(pos) < fps * 2:
        raise ValueError("subject visible for under 2 seconds — cannot analyze")

    pos = pos.sort_values("t").reset_index(drop=True)
    x = _smooth(pos["x"])
    y = _smooth(pos["y"])
    t = pos["t"].to_numpy()

    # movement: per-step displacement, glitch-capped
    dx = np.diff(x)
    dy = np.diff(y)
    dt = np.clip(np.diff(t), 1e-3, None)
    step = np.hypot(dx, dy)
    speed = step / dt
    valid = speed <= MAX_SPEED_FT_S
    distance_ft = float(step[valid].sum())
    active_s = float(t[-1] - t[0])
    avg_speed = distance_ft / active_s if active_s > 0 else 0.0
    live = _live_mask(t, play_windows)
    use_live = int(live.sum()) >= MIN_LIVE_SAMPLES
    movement_curve = _movement_curve(t[1:], speed, valid & live[1:] if use_live else valid)

    # zone occupancy (live play only when there is enough of it)
    zones = pd.Series([zone_for(v) for v in (y[live] if use_live else y)])
    zone_pct = (zones.value_counts(normalize=True) * 100).round(1).to_dict()
    for z in ("kitchen", "transition", "baseline"):
        zone_pct.setdefault(z, 0.0)

    # heatmap grid (GRID_L rows = along court length, GRID_W cols = width)
    gx = np.clip((x / CELL_FT).astype(int), 0, GRID_W - 1)
    gy = np.clip((y / CELL_FT).astype(int), 0, GRID_L - 1)
    grid = np.zeros((GRID_L, GRID_W), dtype=int)
    np.add.at(grid, (gy, gx), 1)

    # court coverage: fraction of subject's half actually visited
    med_side_far = float(np.median(y)) < 22.0
    half = grid[: GRID_L // 2] if med_side_far else grid[GRID_L // 2 :]
    coverage_pct = round(100.0 * float((half > 0).mean()), 1)

    median_net = float(np.median([dist_from_net(v) for v in y]))
    warnings = []
    if median_net > 22.0:  # baseline is 22 ft out: beyond it means bad geometry
        warnings.append(
            "Subject projects behind the baseline most of the video — court corners "
            "are likely misplaced or the wrong person was selected. Recalibrate "
            "(marking the kitchen corners helps a lot).")
    if camera_cuts > 3:
        warnings.append(
            f"{camera_cuts} camera cuts detected — this looks like broadcast/edited "
            "footage. A fixed tripod angle gives much more reliable analysis.")
    if play_windows and not use_live:
        warnings.append(
            "Very little live play was detected — positioning is measured over all tracked "
            "time, including walking between points, so it says less about how you play.")
    if secondary_court_tracks > 0:
        warnings.append(
            "Multiple players detected on court — doubles/crowded court isn't "
            "fully supported yet, so opponent stats may be unreliable.")

    return {
        "frames_analyzed": int(len(pos)),
        "active_seconds": round(active_s, 1),
        "distance_ft": round(distance_ft, 1),
        "avg_speed_ft_s": round(avg_speed, 2),
        "zone_pct": zone_pct,
        "zone_scope": "live" if use_live else "all",
        "live_seconds": round(float(live.sum() * np.median(np.diff(t))), 1) if use_live else 0.0,
        "median_dist_from_net_ft": round(median_net, 1),
        "coverage_pct": coverage_pct,
        "heatmap": grid.tolist(),
        "heatmap_cell_ft": CELL_FT,
        "movement_curve": movement_curve,
        "camera_cuts": camera_cuts,
        "secondary_court_tracks": secondary_court_tracks,
        "warnings": warnings,
    }
