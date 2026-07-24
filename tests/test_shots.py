"""Shot classification rule tests."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shots import (ball_speed_at, classify_shot, mph_from_norm,
                   opponent_shot_landings, opponent_shot_report, serve_metrics,
                   serve_side, shot_report, subject_pos_at, swing_speed_at)

FPS = 30.0
CORNERS = [[300.0, 150.0], [1000.0, 150.0], [1250.0, 700.0], [50.0, 700.0]]


def test_classify_rules():
    assert classify_shot(2.5, 5.0) == "drive"
    assert classify_shot(0.4, 3.0) == "dink"        # slow, at the kitchen
    assert classify_shot(0.4, 18.0) == "drop"       # slow, from the baseline
    assert classify_shot(1.2, 10.0) == "medium"
    assert classify_shot(None, 5.0) == "unknown"


def test_ball_speed_at():
    frames = np.arange(60)
    ball = pd.DataFrame({"frame": frames, "x": frames * 10.0, "y": 300.0, "seg": 0})
    sp = ball_speed_at(ball, 1.0, FPS)   # 10 px/frame * 30 fps = 300 px/s
    assert sp is not None and abs(sp - 300.0) < 1e-6, sp
    assert ball_speed_at(ball, 10.0, FPS) is None  # outside track


def test_low_coverage_degrades():
    rep = shot_report(np.array([1.0]), np.array([True]), pd.DataFrame(),
                      {"coverage": 0.05}, pd.DataFrame(columns=["t", "y"]),
                      [], CORNERS, pd.DataFrame(columns=["x", "y", "t"]), FPS)
    assert rep["available"] is False and "coverage" in rep["reason"]


def test_serve_depth():
    rallies = [{"start": 1.0, "end": 3.0, "hits": 3, "duration": 2.0}]
    hits = np.array([1.0, 2.0, 3.0])
    mine = np.array([True, False, True])
    # serve bounce lands at court y=40 -> 4 ft from the far baseline (44)
    bounces = pd.DataFrame({"x": [10.0], "y": [40.0], "t": [1.4]})
    m = serve_metrics(rallies, mine, hits, bounces)
    assert m["serves_measured"] == 1
    assert m["avg_serve_depth_from_baseline_ft"] == 4.0
    assert m["deep_serve_pct"] == 100.0


def test_serve_placement_recorded():
    rallies = [{"start": 1.0, "end": 3.0, "hits": 3, "duration": 2.0}]
    hits = np.array([1.0, 2.0, 3.0])
    mine = np.array([True, False, True])
    bounces = pd.DataFrame({"x": [10.0], "y": [40.0], "t": [1.4]})  # court width 20 -> middle third
    m = serve_metrics(rallies, mine, hits, bounces)
    assert m["serves"] == [{"t": 1.0, "depth_ft": 4.0, "x": 10.0, "y": 40.0,
                           "side": "middle", "rally_index": 0}]
    assert m["serve_side_counts"] == {"middle": 1}


def test_serve_rally_index_skips_non_subject_serves():
    # 3 rallies: subject serves rally 0, opponent serves rally 1 (no serve
    # recorded), subject serves rally 2 -- rally_index must track the real
    # position in `rallies`, not the position in the (shorter) serves list
    rallies = [{"start": 1.0, "end": 2.0, "hits": 2, "duration": 1.0},
              {"start": 10.0, "end": 11.0, "hits": 2, "duration": 1.0},
              {"start": 20.0, "end": 21.0, "hits": 2, "duration": 1.0}]
    hits = np.array([1.0, 2.0, 10.0, 11.0, 20.0, 21.0])
    mine = np.array([True, False, False, True, True, False])
    bounces = pd.DataFrame({"x": [10.0, 10.0], "y": [40.0, 40.0], "t": [1.4, 20.4]})
    m = serve_metrics(rallies, mine, hits, bounces)
    assert [s["rally_index"] for s in m["serves"]] == [0, 2]


def test_serve_side_thirds():
    assert serve_side(1.0) == "left"     # < 6.67
    assert serve_side(10.0) == "middle"  # 6.67-13.33
    assert serve_side(19.0) == "right"   # > 13.33


def test_subject_pos_at():
    pos = pd.DataFrame({"t": [0.8, 1.0, 1.2], "x": [5.0, 6.0, 7.0], "y": [10.0, 12.0, 14.0]})
    p = subject_pos_at(pos, 1.0)
    assert p == (6.0, 12.0)
    assert subject_pos_at(pos, 100.0) is None  # nothing within window


def test_shot_report_includes_position_when_available():
    rallies = [{"start": 1.0, "end": 1.0, "hits": 1, "duration": 0.0}]
    hits = np.array([1.0])
    mine = np.array([True])
    frames = np.arange(60)
    ball = pd.DataFrame({"frame": frames, "x": frames * 10.0, "y": 300.0, "seg": 0})
    pos = pd.DataFrame({"t": [0.9, 1.0, 1.1], "x": [8.0, 9.0, 10.0], "y": [15.0, 16.0, 17.0]})
    rep = shot_report(hits, mine, ball, {"coverage": 1.0}, pos, rallies, CORNERS,
                      pd.DataFrame(columns=["x", "y", "t"]), FPS)
    assert rep["available"] is True
    assert len(rep["shots"]) == 1
    assert rep["shots"][0]["x"] == 9.0 and rep["shots"][0]["y"] == 16.0


def test_mph_from_norm():
    assert mph_from_norm(None) is None
    assert mph_from_norm(1.0) == round(20.0 / 1.46667, 1)
    assert mph_from_norm(50.0) is None  # 50 court-widths/s -> ~682mph, a tracking glitch


def test_shot_report_includes_mph():
    rallies = [{"start": 1.0, "end": 1.0, "hits": 1, "duration": 0.0}]
    hits = np.array([1.0])
    mine = np.array([True])
    frames = np.arange(60)
    ball = pd.DataFrame({"frame": frames, "x": frames * 10.0, "y": 300.0, "seg": 0})
    pos = pd.DataFrame({"t": [0.9, 1.0, 1.1], "x": [8.0, 9.0, 10.0], "y": [15.0, 16.0, 17.0]})
    rep = shot_report(hits, mine, ball, {"coverage": 1.0}, pos, rallies, CORNERS,
                      pd.DataFrame(columns=["x", "y", "t"]), FPS)
    # width_px for CORNERS is 950 (avg of the two baselines); speed is 300 px/s
    expected = mph_from_norm(300.0 / 950.0)
    assert expected is not None
    assert rep["shots"][0]["mph"] == expected
    assert rep["avg_shot_mph"] == expected
    assert rep["top_shot_mph"] == expected


def test_swing_speed_at_windowed_median():
    ws = pd.DataFrame({"t": [0.9, 1.0, 1.1], "speed": [1.0, 1.0, 1.0]})
    assert swing_speed_at(ws, 1.0) == mph_from_norm(1.0)
    assert swing_speed_at(ws, 100.0) is None  # nothing within the window


def test_shot_report_includes_swing_mph_when_ws_provided():
    rallies = [{"start": 1.0, "end": 1.0, "hits": 1, "duration": 0.0}]
    hits = np.array([1.0])
    mine = np.array([True])
    frames = np.arange(60)
    ball = pd.DataFrame({"frame": frames, "x": frames * 10.0, "y": 300.0, "seg": 0})
    pos = pd.DataFrame({"t": [0.9, 1.0, 1.1], "x": [8.0, 9.0, 10.0], "y": [15.0, 16.0, 17.0]})
    ws = pd.DataFrame({"t": [0.9, 1.0, 1.1], "speed": [1.0, 1.0, 1.0]})
    rep = shot_report(hits, mine, ball, {"coverage": 1.0}, pos, rallies, CORNERS,
                      pd.DataFrame(columns=["x", "y", "t"]), FPS, ws=ws)
    expected = mph_from_norm(1.0)
    assert rep["shots"][0]["swing_mph"] == expected
    assert rep["avg_swing_mph"] == expected


def test_shot_report_swing_mph_absent_without_ws():
    rallies = [{"start": 1.0, "end": 1.0, "hits": 1, "duration": 0.0}]
    hits = np.array([1.0])
    mine = np.array([True])
    frames = np.arange(60)
    ball = pd.DataFrame({"frame": frames, "x": frames * 10.0, "y": 300.0, "seg": 0})
    pos = pd.DataFrame({"t": [0.9, 1.0, 1.1], "x": [8.0, 9.0, 10.0], "y": [15.0, 16.0, 17.0]})
    rep = shot_report(hits, mine, ball, {"coverage": 1.0}, pos, rallies, CORNERS,
                      pd.DataFrame(columns=["x", "y", "t"]), FPS)
    assert "swing_mph" not in rep["shots"][0]
    assert rep["avg_swing_mph"] is None


def test_shot_report_top_shot_t_points_at_the_fastest_shot():
    # two hits: a slow one at t=1.0, a much faster one at t=5.0 -- top_shot_t
    # must point at the fast one, not just the last shot in the list
    rallies = [{"start": 1.0, "end": 5.0, "hits": 2, "duration": 4.0}]
    hits = np.array([1.0, 5.0])
    mine = np.array([True, True])
    frames_slow = np.arange(25, 46)
    x_slow = (frames_slow - 25) * 1.0          # ~30 px/s
    frames_fast = np.arange(140, 166)
    x_fast = (frames_fast - 140) * 20.0 + 1000.0  # ~600 px/s
    ball = pd.concat([
        pd.DataFrame({"frame": frames_slow, "x": x_slow, "y": 300.0, "seg": 0}),
        pd.DataFrame({"frame": frames_fast, "x": x_fast, "y": 300.0, "seg": 1}),
    ], ignore_index=True)
    pos = pd.DataFrame(columns=["t", "x", "y"])
    rep = shot_report(hits, mine, ball, {"coverage": 1.0}, pos, rallies, CORNERS,
                      pd.DataFrame(columns=["x", "y", "t"]), FPS)
    assert rep["top_shot_t"] == 5.0
    assert rep["top_shot_mph"] > rep["shots"][0]["mph"]


def test_opponent_shot_landings_and_report():
    hit_times = np.array([1.0, 2.0, 3.0])
    hitters = ["subject", "opponent1", "opponent1"]
    bounces = pd.DataFrame({"x": [5.0, 15.0], "y": [10.0, 30.0], "t": [2.3, 3.2]})
    landings = opponent_shot_landings(hit_times, hitters, bounces, "opponent1")
    assert landings == [
        {"t": 2.0, "x": 5.0, "y": 10.0, "side": "left"},
        {"t": 3.0, "x": 15.0, "y": 30.0, "side": "right"},
    ]
    rep = opponent_shot_report(hit_times, hitters, bounces)
    assert rep["shots_tracked"] == 2
    assert rep["side_counts"] == {"left": 1, "right": 1}
    assert rep["dominant_side"] == "left"
    assert rep["dominant_side_pct"] == 50.0


def test_opponent_shot_report_empty_when_no_hits():
    rep = opponent_shot_report(np.array([]), [], pd.DataFrame(columns=["x", "y", "t"]))
    assert rep == {"shots_tracked": 0}


def test_shot_report_with_hitters_adds_opponent_shots():
    rallies = [{"start": 1.0, "end": 1.0, "hits": 1, "duration": 0.0}]
    hits = np.array([1.0])
    mine = np.array([True])
    frames = np.arange(60)
    ball = pd.DataFrame({"frame": frames, "x": frames * 10.0, "y": 300.0, "seg": 0})
    pos = pd.DataFrame({"t": [0.9, 1.0, 1.1], "x": [8.0, 9.0, 10.0], "y": [15.0, 16.0, 17.0]})
    rep = shot_report(hits, mine, ball, {"coverage": 1.0}, pos, rallies, CORNERS,
                      pd.DataFrame(columns=["x", "y", "t"]), FPS, hitters=["subject"])
    assert rep["opponent_shots"] == {"shots_tracked": 0}


def test_serve_depth_rejects_implausible_bounce():
    # a single-frame glitch bounce (y=300 -- nowhere near the court, which
    # only runs 0-44ft) must not be reported as a real serve landing
    rallies = [{"start": 1.0, "end": 3.0, "hits": 3, "duration": 2.0}]
    hits = np.array([1.0, 2.0, 3.0])
    mine = np.array([True, False, True])
    bounces = pd.DataFrame({"x": [10.0], "y": [300.0], "t": [1.4]})
    m = serve_metrics(rallies, mine, hits, bounces)
    assert m == {"serves_measured": 0}


def test_serve_depth_picks_plausible_bounce_over_glitch():
    # a glitch bounce earlier in the window must not shadow the real one
    rallies = [{"start": 1.0, "end": 3.0, "hits": 3, "duration": 2.0}]
    hits = np.array([1.0, 2.0, 3.0])
    mine = np.array([True, False, True])
    bounces = pd.DataFrame({"x": [10.0, 10.0], "y": [-200.0, 40.0], "t": [1.1, 1.4]})
    m = serve_metrics(rallies, mine, hits, bounces)
    assert m["serves_measured"] == 1
    assert m["avg_serve_depth_from_baseline_ft"] == 4.0


def test_opponent_shot_landings_rejects_implausible_bounce():
    hit_times = np.array([1.0])
    hitters = ["opponent1"]
    bounces = pd.DataFrame({"x": [5.0], "y": [-500.0], "t": [1.3]})
    assert opponent_shot_landings(hit_times, hitters, bounces, "opponent1") == []


def test_opponent_shot_report_combines_opponent1_and_opponent2():
    hit_times = np.array([1.0, 2.0])
    hitters = ["opponent1", "opponent2"]
    bounces = pd.DataFrame({"x": [5.0, 15.0], "y": [10.0, 30.0], "t": [1.3, 2.3]})
    rep = opponent_shot_report(hit_times, hitters, bounces)
    assert rep["shots_tracked"] == 2
    assert rep["side_counts"] == {"left": 1, "right": 1}


if __name__ == "__main__":
    for fn in [test_classify_rules, test_ball_speed_at, test_low_coverage_degrades,
               test_serve_depth, test_serve_placement_recorded,
               test_serve_rally_index_skips_non_subject_serves, test_serve_side_thirds,
               test_subject_pos_at, test_shot_report_includes_position_when_available,
               test_mph_from_norm, test_shot_report_includes_mph,
               test_swing_speed_at_windowed_median,
               test_shot_report_includes_swing_mph_when_ws_provided,
               test_shot_report_swing_mph_absent_without_ws,
               test_opponent_shot_landings_and_report, test_opponent_shot_report_empty_when_no_hits,
               test_shot_report_with_hitters_adds_opponent_shots,
               test_serve_depth_rejects_implausible_bounce,
               test_serve_depth_picks_plausible_bounce_over_glitch,
               test_opponent_shot_landings_rejects_implausible_bounce,
               test_opponent_shot_report_combines_opponent1_and_opponent2,
               test_shot_report_top_shot_t_points_at_the_fastest_shot]:
        fn()
        print(f"ok {fn.__name__}")
