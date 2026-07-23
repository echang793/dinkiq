"""Ball tracker tests: synthetic video with a known moving-ball trajectory."""

import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ball import BallDetector, detect_bounces, detect_cuts, link_tracks, run_ball_tracking

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


def test_ball_detector_color_gate_accepts_bright_rejects_dark():
    # dark, static background -- gives MOG2 a stable model before the ball appears
    bg = np.full((200, 200, 3), 40, dtype=np.uint8)
    det_yellow, det_gray = BallDetector(), BallDetector()
    for f in range(15):
        det_yellow.update(f, bg.copy())
        det_gray.update(f, bg.copy())

    yellow_frame = bg.copy()
    cv2.circle(yellow_frame, (100, 100), 6, (0, 255, 255), -1)  # BGR yellow: bright + saturated
    gray_frame = bg.copy()
    cv2.circle(gray_frame, (100, 100), 6, (90, 90, 90), -1)     # same size/shape, too dark

    det_yellow.update(15, yellow_frame)
    det_gray.update(15, gray_frame)

    assert any(f == 15 for f, *_ in det_yellow.candidates), det_yellow.candidates
    assert not any(f == 15 for f, *_ in det_gray.candidates), det_gray.candidates


def test_detect_cuts_finds_hard_scene_change():
    with tempfile.TemporaryDirectory() as td:
        vid = Path(td) / "cut.mp4"
        vw = cv2.VideoWriter(str(vid), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
        court_green = np.full((H, W, 3), (60, 140, 60), dtype=np.uint8)
        crowd_gray = np.full((H, W, 3), (150, 150, 150), dtype=np.uint8)
        for f in range(60):
            vw.write(court_green if f < 30 else crowd_gray)  # hard cut at frame 30
        vw.release()

        cuts = detect_cuts(vid)
        assert cuts, "expected at least one detected cut"
        assert any(abs(c - 30) <= 2 for c in cuts), cuts


def test_link_ignores_isolated_noise():
    # 3 isolated single-frame blips + one real 8-frame run
    cands = [(0, 500, 500, 20), (10, 900, 100, 20), (20, 30, 30, 20)]
    cands += [(30 + k, 100 + 10 * k, 200, 20) for k in range(8)]
    df = link_tracks(cands, 50)
    assert df["seg"].nunique() == 1, df
    assert len(df) == 8


if __name__ == "__main__":
    for fn in [test_tracks_and_bounces, test_ball_detector_color_gate_accepts_bright_rejects_dark,
              test_detect_cuts_finds_hard_scene_change, test_link_ignores_isolated_noise]:
        fn()
        print(f"ok {fn.__name__}")
