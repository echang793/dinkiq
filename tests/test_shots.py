"""Shot classification rule tests."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shots import (ball_speed_at, classify_shot, serve_metrics, shot_report)

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


if __name__ == "__main__":
    for fn in [test_classify_rules, test_ball_speed_at, test_low_coverage_degrades,
               test_serve_depth]:
        fn()
        print(f"ok {fn.__name__}")
