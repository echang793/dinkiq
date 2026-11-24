"""Positioning is judged during live play, not while walking between points.

zone_pct (kitchen / transition / baseline), the movement curve and the fatigue note
counted every tracked frame -- on the sample ~98% of the video was NOT in a rally
(play_time_pct was 2.0), so the "kitchen discipline" that carries 30% of the rating
mostly measured where the subject waited. Also, subject positions more than 4 ft
past a baseline were discarded as "off court", but returners routinely stand 2-6 ft
behind it, so baseline time was undercounted and kitchen time inflated.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from court import CourtCalibration, on_court_mask
from metrics import compute_metrics
from tracking import subject_court_positions

FPS = 30.0
HZ = 15.0   # tracking samples per second


def _pos(duration_s, y_of_t, x=10.0):
    t = np.arange(0, duration_s, 1.0 / HZ)
    return pd.DataFrame({"frame": (t * FPS).astype(int), "t": t, "x": x, "y": [y_of_t(v) for v in t]})


def camps_at_kitchen_between_points():
    # kitchen line (y=16) for everything except a 30 s rally played from the baseline (y=40)
    return _pos(200, lambda t: 40.0 if 100 <= t < 130 else 16.0)


def test_with_no_windows_every_sample_counts_as_before():
    m = compute_metrics(camps_at_kitchen_between_points(), FPS)
    assert m["zone_scope"] == "all" and m["zone_pct"]["kitchen"] > 80


def test_live_windows_measure_where_the_player_actually_plays():
    m = compute_metrics(camps_at_kitchen_between_points(), FPS, play_windows=[(100.0, 130.0)])
    assert m["zone_scope"] == "live"
    assert m["zone_pct"]["baseline"] > 90 and m["zone_pct"]["kitchen"] < 10
    assert 28 <= m["live_seconds"] <= 36        # the window plus its 1 s padding each side


def test_a_window_pads_a_second_either_side_to_include_the_approach():
    # arrives at the kitchen 0.5 s before the first hit: padding includes it
    pos = _pos(200, lambda t: 16.0 if 99.0 <= t < 131.0 else 40.0)
    m = compute_metrics(pos, FPS, play_windows=[(100.0, 130.0)])
    assert m["zone_pct"]["kitchen"] > 95


def test_too_little_live_play_falls_back_to_all_time_and_says_so():
    m = compute_metrics(camps_at_kitchen_between_points(), FPS, play_windows=[(100.0, 102.0)])   # 4 s with padding
    assert m["zone_scope"] == "all"
    assert any("live play" in w.lower() for w in m["warnings"])


def test_non_zone_metrics_still_cover_the_whole_session():
    all_t = compute_metrics(camps_at_kitchen_between_points(), FPS)
    live = compute_metrics(camps_at_kitchen_between_points(), FPS, play_windows=[(100.0, 130.0)])
    for key in ("distance_ft", "avg_speed_ft_s", "active_seconds", "coverage_pct", "heatmap"):
        assert all_t[key] == live[key], key


def test_the_movement_curve_is_also_live_only():
    pos = _pos(200, lambda t: 16.0 + (8.0 * np.sin(t * 2.0) if 100 <= t < 130 else 0.0))   # moves only in the rally
    live = compute_metrics(pos, FPS, play_windows=[(100.0, 130.0)])["movement_curve"]
    whole = compute_metrics(pos, FPS)["movement_curve"]
    assert all(100 - 61 <= c["t_start"] <= 130 for c in live)
    assert len(live) < len(whole)


def test_an_empty_window_list_is_the_same_as_none():
    a = compute_metrics(camps_at_kitchen_between_points(), FPS, play_windows=[])
    assert a["zone_scope"] == "all"


# ── positions behind the baseline are real ──────────────────────────────

CAMERA = [[400.0, 200.0], [880.0, 200.0], [1180.0, 680.0], [100.0, 680.0]]


def test_on_court_mask_allows_a_deeper_margin_behind_the_baseline_only():
    pts = np.array([[10.0, 50.0], [10.0, 52.5], [26.0, 20.0], [10.0, -7.0], [10.0, -9.0]])
    assert on_court_mask(pts, margin=4.0, margin_y=8.0).tolist() == [True, False, False, True, False]
    assert on_court_mask(pts, margin=4.0).tolist() == [False, False, False, False, False]   # old rule: 4 ft all round


def test_a_returner_6ft_behind_the_baseline_stays_in_the_positions():
    calib = CourtCalibration(CAMERA)
    inv = np.linalg.inv(calib.H)

    def px(court_x, court_y):
        p = inv @ np.array([court_x, court_y, 1.0])
        return p[0] / p[2], p[1] / p[2]

    fx, fy = px(10.0, 50.0)                               # 6 ft behind the near baseline
    n = 60
    tracks = pd.DataFrame({"frame": np.arange(n) * 2, "track_id": 1, "x1": fx - 20, "x2": fx + 20,
                           "y1": fy - 150, "y2": fy, "conf": 0.9, "lwx": 0.0, "lwy": 0.0, "rwx": 0.0, "rwy": 0.0})
    pos = subject_court_positions(tracks, 1, calib, FPS)
    assert len(pos) == n and abs(pos["y"].median() - 50.0) < 0.5
