"""The ball track must not claim coverage it hasn't earned.

On the sample the "ball" track had 5,579 segments, a median of 12 linked points per
frame and one segment 1,835 frames long (a static object), yet reported 96% coverage
-- so every ball-derived metric passed its gate on noise. Also: gap bridging could
link points 960 px apart, ball speed pooled points from different segments (a
static blob next to a real ball), and the legacy fallback decode mixed strides so
coverage could exceed 100%.
"""

import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import ball as ballmod
from ball import build_ball_track, link_tracks, run_ball_tracking, validate_segments
from shots import ball_speed_at

FPS = 30.0


def _seg(seg, frames, x0, y0, dx, dy=0.0):
    frames = np.asarray(frames)
    k = np.arange(len(frames))
    return pd.DataFrame({"frame": frames, "x": x0 + dx * k, "y": y0 + dy * k, "seg": seg})


# ── segment validation ─────────────────────────────────────────────────

def test_a_segment_that_never_moves_is_not_a_ball():
    static = _seg(0, range(0, 600, 2), 640.0, 360.0, 0.0)           # a logo / court mark, 300 samples
    assert len(validate_segments(static)) == 0


def test_a_segment_that_barely_drifts_is_not_a_ball():
    drift = _seg(0, range(0, 80, 2), 640.0, 360.0, 0.1)             # 40 samples, 4 px in total
    assert len(validate_segments(drift)) == 0


def test_a_real_flight_is_kept():
    flight = _seg(0, range(100, 160, 2), 100.0, 500.0, 12.0, -3.0)  # 30 samples, ~370 px across
    assert len(validate_segments(flight)) == len(flight)


def test_an_implausibly_long_segment_is_dropped_even_if_it_moves():
    """One 'ball' that lasts 20 s is a chain of unrelated blobs, not a flight."""
    wander = _seg(0, range(0, 600, 2), 50.0, 360.0, 3.0)
    assert len(validate_segments(wander)) == 0


def test_validation_judges_each_segment_independently():
    keep = _seg(1, range(100, 160, 2), 100.0, 500.0, 12.0)
    junk = _seg(2, range(0, 400, 2), 640.0, 360.0, 0.0)
    out = validate_segments(pd.concat([keep, junk], ignore_index=True))
    assert set(out["seg"]) == {1} and len(out) == len(keep)


def test_empty_input_is_fine():
    assert len(validate_segments(pd.DataFrame(columns=["frame", "x", "y", "seg"]))) == 0


# ── coverage is earned, not accumulated ────────────────────────────────

def test_a_static_blob_in_every_frame_gives_zero_coverage():
    cands = [(f, 640.0, 360.0, 30.0) for f in range(0, 600, 2)]        # the same spot, every sample
    with tempfile.TemporaryDirectory() as td:
        ball, stats = build_ball_track(cands, 600, Path(td) / "b.parquet", stride=2)
    assert stats["coverage"] == 0.0
    assert stats["segments"] == 0 and stats["raw_segments"] >= 1


def test_coverage_counts_only_frames_with_a_validated_ball():
    flight = [(f, 100.0 + 12.0 * k, 500.0 - 3.0 * k, 30.0) for k, f in enumerate(range(100, 160, 2))]
    static = [(f, 900.0, 200.0, 30.0) for f in range(0, 600, 2)]
    with tempfile.TemporaryDirectory() as td:
        ball, stats = build_ball_track(flight + static, 600, Path(td) / "b.parquet", stride=2)
    assert 0.08 < stats["coverage"] < 0.12, stats                       # 30 of 300 samples, not 100%
    assert stats["segments"] == 1


# ── linking ────────────────────────────────────────────────────────────

def test_gap_bridging_cannot_jump_hundreds_of_pixels():
    """After a few missed frames the old matching radius grew to 120 px x elapsed frames
    (up to 960 px): an unrelated blob across the frame got linked into the track."""
    track = [(f, 100.0 + 10.0 * f, 300.0, 20.0) for f in range(0, 6)]   # ends at x=150, frame 5
    stray = [(9, 550.0, 300.0, 20.0)]                                    # 400 px away, 4 frames later
    out = link_tracks(track + stray, 20, stride=1)
    assert not ((out["x"] > 400)).any(), "the distant blob was linked into the ball's track"


def test_a_modest_jump_after_a_short_gap_still_links():
    track = [(f, 100.0 + 10.0 * f, 300.0, 20.0) for f in range(0, 6)]
    resumed = [(8, 100.0 + 10.0 * 8, 300.0, 20.0), (9, 190.0, 300.0, 20.0), (10, 200.0, 300.0, 20.0)]
    out = link_tracks(track + resumed, 20, stride=1)
    assert out["seg"].nunique() == 1 and out["frame"].max() == 10


# ── speed ──────────────────────────────────────────────────────────────

def test_ball_speed_uses_one_segment_not_a_pool_of_all_of_them():
    """A static blob and a real ball in the same window: pooled and sorted by frame, the
    consecutive differences alternate between the two and the 'speed' is garbage."""
    real = _seg(0, range(30, 46), 100.0, 300.0, 10.0)                  # 16 points, 10 px/frame
    blob = _seg(1, range(30, 46, 4), 900.0, 100.0, 0.0)                # 4 points elsewhere
    ball = pd.concat([real, blob], ignore_index=True)
    sp = ball_speed_at(ball, 30 / FPS, FPS)
    assert sp is not None and abs(sp - 10.0 * FPS) < 1.0, sp


def test_ball_speed_is_none_when_no_single_segment_has_enough_points():
    a = _seg(0, [30, 32], 100.0, 300.0, 10.0)
    b = _seg(1, [31, 33], 500.0, 300.0, 10.0)
    assert ball_speed_at(pd.concat([a, b]), 30 / FPS, FPS) is None


# ── legacy fallback decode keeps one stride ────────────────────────────

def test_fallback_ball_pass_uses_the_tracking_stride_so_coverage_cannot_exceed_one():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_ball import make_ball_video
    with tempfile.TemporaryDirectory() as td:
        vid = Path(td) / "ball.mp4"
        make_ball_video(vid)
        ball, stats = run_ball_tracking(vid, pd.DataFrame(), Path(td) / "b.parquet", FPS, stride=2)
    assert stats["stride"] == 2 and 0 < stats["coverage"] <= 1.0, stats
    assert (ball["frame"] % 2 == 0).all()


def test_the_constants_are_exposed_for_tuning():
    assert ballmod.MAX_SEGMENT_FRAMES > 0 and ballmod.MIN_SEGMENT_NET_PX > 0 and ballmod.MIN_SEGMENT_SPEED_PX > 0


# ── the track must beat chance at the moments that matter ──────────────

from shots import ball_presence, shot_report  # noqa: E402

CORNERS = [[300.0, 150.0], [1000.0, 150.0], [1250.0, 700.0], [50.0, 700.0]]


def _flight_after(t, seg):
    f0 = int(t * FPS)
    return _seg(seg, range(f0, f0 + 12), 100.0, 300.0, 14.0)


def test_a_ball_track_that_appears_after_hits_and_not_elsewhere_is_trusted():
    hits = np.array([5.0, 12.0, 20.0, 31.0, 40.0, 52.0])
    ball = pd.concat([_flight_after(t, i) for i, t in enumerate(hits)], ignore_index=True)
    p = ball_presence(ball, hits, duration_s=60.0, fps=FPS)
    assert p["hit_rate"] == 1.0 and p["chance_rate"] < 0.3 and p["lift"] > 3.0


def test_a_ball_track_that_is_everywhere_is_no_evidence_at_all():
    """Busy background: moving blobs at every moment, so 'a ball after the hit' is no
    more likely than a ball at a random time."""
    hits = np.array([5.0, 12.0, 20.0, 31.0, 40.0, 52.0])
    ball = pd.concat([_seg(i, range(i * 30, i * 30 + 28), 100.0, 300.0, 14.0) for i in range(60)], ignore_index=True)
    p = ball_presence(ball, hits, duration_s=60.0, fps=FPS)
    assert p["lift"] < 1.3, p


def test_presence_is_reproducible():
    hits = np.array([5.0, 12.0, 20.0])
    ball = _flight_after(5.0, 0)
    assert ball_presence(ball, hits, 60.0, FPS) == ball_presence(ball, hits, 60.0, FPS)


def _report(ball, hits, duration=60.0, coverage=0.9):
    rallies = [{"start": float(hits[0]), "end": float(hits[-1]), "hits": len(hits), "duration": 1.0}]
    pos = pd.DataFrame({"t": np.linspace(0, duration, 50), "x": 10.0, "y": 30.0})
    stats = {"coverage": coverage, "segments": int(ball["seg"].nunique()), "frames": int(duration * FPS), "stride": 1}
    return shot_report(hits, np.ones(len(hits), bool), ball, stats, pos, rallies, CORNERS,
                       pd.DataFrame(columns=["x", "y", "t"]), FPS)


def test_ball_metrics_are_withheld_when_the_track_does_not_beat_chance():
    hits = np.array([5.0, 12.0, 20.0, 31.0, 40.0, 52.0])
    everywhere = pd.concat([_seg(i, range(i * 30, i * 30 + 28), 100.0, 300.0, 14.0) for i in range(60)], ignore_index=True)
    rep = _report(everywhere, hits)
    assert rep["available"] is False
    assert "random" in rep["reason"] and rep["ball"]["hit_ball_lift"] < 1.3


def test_ball_metrics_are_reported_when_the_track_is_real():
    hits = np.array([5.0, 12.0, 20.0, 31.0, 40.0, 52.0])
    ball = pd.concat([_flight_after(t, i) for i, t in enumerate(hits)], ignore_index=True)
    rep = _report(ball, hits)
    assert rep["available"] is True and rep["ball"]["hit_ball_rate"] == 1.0


def test_with_too_few_hits_to_judge_the_gate_stays_out_of_the_way():
    """Fewer than 5 hits can't establish a rate; the existing coverage gate still applies."""
    hits = np.array([5.0, 12.0])
    everywhere = pd.concat([_seg(i, range(i * 30, i * 30 + 28), 100.0, 300.0, 14.0) for i in range(60)], ignore_index=True)
    assert _report(everywhere, hits)["available"] is True
