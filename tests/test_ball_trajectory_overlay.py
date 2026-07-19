"""shots_stage persists pixel-space bounce points for the trajectory overlay."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pipeline
from court import CourtCalibration

FPS = 30.0
CORNERS = [[300.0, 150.0], [1000.0, 150.0], [1250.0, 700.0], [50.0, 700.0]]


def _bouncy_ball() -> pd.DataFrame:
    """One clean arc: y rises to a peak (ball lowest on screen) then falls —
    exactly what ball.detect_bounces looks for."""
    frames = list(range(11))
    ys = [100, 150, 200, 250, 280, 300, 280, 250, 200, 150, 100]
    xs = [500 + 5 * f for f in frames]
    return pd.DataFrame({"frame": frames, "x": xs, "y": ys, "seg": 0})


def test_shots_stage_persists_pixel_bounces(tmp_path):
    calib = CourtCalibration(CORNERS)
    ball = _bouncy_ball()
    pos = pd.DataFrame({"t": [0.0, 0.3], "x": [10.0, 10.0], "y": [16.0, 16.0]})
    bounces_court = pipeline.shots_stage(
        tmp_path, calib, pos, np.array([]), np.array([]), [], CORNERS,
        ball, {"coverage": 0.0, "segments": 1, "frames": 11, "stride": 1})
    assert len(bounces_court) >= 1  # sanity: a bounce really was detected

    import json
    report = json.loads((tmp_path / "shots.json").read_text())
    assert "bounces_px" in report
    assert len(report["bounces_px"]) >= 1
    b = report["bounces_px"][0]
    assert set(b.keys()) == {"frame", "x", "y", "t"}
    assert b["y"] == 300.0  # the arc's peak, in raw pixel space (not court feet)


def test_shots_stage_empty_bounces_when_no_ball_track(tmp_path):
    calib = CourtCalibration(CORNERS)
    pos = pd.DataFrame({"t": [0.0], "x": [10.0], "y": [16.0]})
    pipeline.shots_stage(tmp_path, calib, pos, np.array([]), np.array([]), [], CORNERS,
                         pd.DataFrame(columns=["frame", "x", "y", "seg"]),
                         {"coverage": 0.0, "segments": 0, "frames": 0, "stride": 1})
    import json
    report = json.loads((tmp_path / "shots.json").read_text())
    assert report["bounces_px"] == []


if __name__ == "__main__":
    import tempfile
    for fn in [test_shots_stage_persists_pixel_bounces, test_shots_stage_empty_bounces_when_no_ball_track]:
        with tempfile.TemporaryDirectory() as td:
            fn(Path(td))
        print(f"ok {fn.__name__}")
