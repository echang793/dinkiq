"""Ball tracker tests: synthetic video with a known moving-ball trajectory."""

import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ball import detect_bounces, link_tracks, run_ball_tracking

FPS = 30
W, H = 1280, 720


def make_ball_video(path: Path, n_frames: int = 90) -> list[tuple[int, float, float]]:
    """Static noisy background + white ball on a bouncing parabolic path."""
    rng = np.random.default_rng(0)
    bg = rng.integers(30, 60, (H, W, 3), dtype=np.uint8)
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
    truth = []
    for f in range(n_frames):
        frame = bg.copy()
        if 10 <= f < 80:  # ball flies for 70 frames
            k = f - 10
            x = 100 + k * 15.0
            # two bounce arcs: parabola dipping to y=600 at k=20 and k=55
            seg, k0 = (0, 0) if k < 35 else (1, 35)
            kk = k - k0
            vertex_k = 20 if seg == 0 else 20
            y = 600 - 0.9 * (kk - vertex_k) ** 2
            y = min(600.0, max(80.0, y))
            cv2.circle(frame, (int(x), int(y)), 7, (255, 255, 255), -1)
            truth.append((f, x, y))
        vw.write(frame)
    vw.release()
    return truth


def test_tracks_and_bounces():
    with tempfile.TemporaryDirectory() as td:
        vid = Path(td) / "ball.mp4"
        truth = make_ball_video(vid)
        ball, stats = run_ball_tracking(vid, pd.DataFrame(), Path(td) / "b.parquet", FPS)

        assert stats["coverage"] > 0.4, stats  # most airborne frames tracked
        # tracked points near ground truth
        tmap = {f: (x, y) for f, x, y in truth}
        errs = [np.hypot(r.x - tmap[r.frame][0], r.y - tmap[r.frame][1])
                for r in ball.itertuples() if r.frame in tmap]
        assert errs and float(np.median(errs)) < 10, np.median(errs)

        bounces = detect_bounces(ball, FPS)
        assert len(bounces) >= 1, bounces  # at least one arc vertex found
        # bounce y near the ground line (600)
        assert (bounces["y"] > 550).any(), bounces


def test_link_ignores_isolated_noise():
    # 3 isolated single-frame blips + one real 8-frame run
    cands = [(0, 500, 500, 20), (10, 900, 100, 20), (20, 30, 30, 20)]
    cands += [(30 + k, 100 + 10 * k, 200, 20) for k in range(8)]
    df = link_tracks(cands, 50)
    assert df["seg"].nunique() == 1, df
    assert len(df) == 8


if __name__ == "__main__":
    for fn in [test_tracks_and_bounces, test_link_ignores_isolated_noise]:
        fn()
        print(f"ok {fn.__name__}")
