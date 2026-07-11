"""Metrics tests on synthetic subject tracks with known ground truth."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from metrics import compute_metrics

FPS = 30.0


def make_track(ys: list[float], x: float = 10.0) -> pd.DataFrame:
    """Stationary-x track visiting given y positions, 1 s (30 frames) each."""
    frames, xs, ys_out = [], [], []
    f = 0
    for y in ys:
        for _ in range(int(FPS)):
            frames.append(f)
            xs.append(x)
            ys_out.append(y)
            f += 1
    return pd.DataFrame({"frame": frames, "t": np.array(frames) / FPS, "x": xs, "y": ys_out})


def test_zone_split():
    # 2 s at kitchen line (y=16, 6 ft from net), 1 s no-man's land (y=10), 1 s baseline (y=2)
    pos = make_track([16.0, 16.0, 10.0, 2.0])
    m = compute_metrics(pos, FPS)
    z = m["zone_pct"]
    # smoothing blurs single-frame boundaries; allow small tolerance
    assert abs(z["kitchen"] - 50.0) < 5, z
    assert abs(z["transition"] - 25.0) < 5, z
    assert abs(z["baseline"] - 25.0) < 5, z


def test_distance_glitch_capped():
    pos = make_track([16.0, 16.0])
    # inject teleport glitch: one frame jumps 30 ft (impossible at 30 fps)
    pos.loc[30, "y"] = 46.0
    m = compute_metrics(pos, FPS)
    assert m["distance_ft"] < 20.0, m["distance_ft"]  # glitch step excluded


def test_determinism():
    pos = make_track([16.0, 10.0, 2.0, 16.0])
    assert compute_metrics(pos, FPS) == compute_metrics(pos, FPS)


def test_heatmap_shape_and_mass():
    pos = make_track([16.0, 10.0])
    m = compute_metrics(pos, FPS)
    grid = np.array(m["heatmap"])
    assert grid.shape == (22, 10)
    assert grid.sum() == len(pos)


def test_too_short_rejected():
    pos = make_track([16.0]).head(10)
    try:
        compute_metrics(pos, FPS)
        raise AssertionError("should have raised")
    except ValueError:
        pass


if __name__ == "__main__":
    for fn in [test_zone_split, test_distance_glitch_capped, test_determinism,
               test_heatmap_shape_and_mass, test_too_short_rejected]:
        fn()
        print(f"ok {fn.__name__}")
