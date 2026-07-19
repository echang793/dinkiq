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


def test_secondary_court_tracks_warns():
    pos = make_track([16.0, 16.0])
    clean = compute_metrics(pos, FPS, secondary_court_tracks=0)
    doubles = compute_metrics(pos, FPS, secondary_court_tracks=1)
    assert clean["secondary_court_tracks"] == 0
    assert not any("Multiple players" in w for w in clean["warnings"])
    assert doubles["secondary_court_tracks"] == 1
    assert any("Multiple players" in w for w in doubles["warnings"])


def test_movement_curve_separates_fast_and_slow_windows():
    # bucket 0 (t 0-60s): brisk lateral movement every frame
    # bucket 1 (t 60-120s): standing nearly still
    n_fast, n_slow = int(60 * FPS), int(60 * FPS)
    fast_x = 10.0 + 0.05 * np.sin(np.arange(n_fast) * 0.5)  # oscillates -> real movement
    slow_x = np.full(n_slow, 10.0)  # stationary
    frames = np.arange(n_fast + n_slow)
    x = np.concatenate([fast_x, slow_x])
    pos = pd.DataFrame({"frame": frames, "t": frames / FPS, "x": x, "y": 16.0})
    m = compute_metrics(pos, FPS)
    curve = m["movement_curve"]
    by_bucket = {c["t_start"]: c["avg_speed_ft_s"] for c in curve}
    assert 0.0 in by_bucket
    assert by_bucket[0.0] > by_bucket.get(60.0, 0.0)


def test_movement_curve_empty_for_short_track():
    pos = make_track([16.0, 16.0])  # ~2s, no buckets can accumulate MIN_BUCKET_SAMPLES gaps
    m = compute_metrics(pos, FPS)
    assert isinstance(m["movement_curve"], list)


if __name__ == "__main__":
    for fn in [test_zone_split, test_distance_glitch_capped, test_determinism,
               test_heatmap_shape_and_mass, test_too_short_rejected,
               test_secondary_court_tracks_warns,
               test_movement_curve_separates_fast_and_slow_windows,
               test_movement_curve_empty_for_short_track]:
        fn()
        print(f"ok {fn.__name__}")
