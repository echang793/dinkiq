"""Subject stitching + kitchen-anchored homography tests."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from court import NET_Y, CourtCalibration
from tracking import feet_px, pick_opponent, stitch_chain_ids, stitch_subject


def _frag(tid: int, f0: int, n: int, x0: float, y0: float, dx: float = 2.0) -> pd.DataFrame:
    frames = np.arange(f0, f0 + n)
    x1 = x0 + dx * (frames - f0)
    return pd.DataFrame({"frame": frames, "track_id": tid,
                         "x1": x1, "y1": y0, "x2": x1 + 40, "y2": y0 + 120,
                         "conf": 0.9})


def test_stitch_follows_across_id_breaks():
    # subject walks right; tracker re-ids twice with small gaps
    a = _frag(1, 0, 50, 100, 300)
    b = _frag(7, 55, 50, 100 + 2 * 55, 300)   # resumes on-path after 5-frame gap
    c = _frag(9, 110, 50, 100 + 2 * 110, 300)
    far = _frag(4, 55, 100, 900, 100)          # different person far away
    df = pd.concat([a, far, b, c], ignore_index=True)
    out = stitch_subject(df, 1)
    assert set(out["track_id"].unique()) == {1, 7, 9}, out["track_id"].unique()
    assert 4 not in out["track_id"].to_numpy()
    assert len(out) == 150


def test_stitch_respects_gap_and_distance():
    a = _frag(1, 0, 30, 100, 300)
    teleport = _frag(2, 33, 30, 1100, 600)    # nearby in time, far in space
    late = _frag(3, 120, 30, 160, 300)        # on-path but 60-frame gap (too long)
    df = pd.concat([a, teleport, late], ignore_index=True)
    out = stitch_subject(df, 1)
    assert set(out["track_id"].unique()) == {1}


def test_kitchen_anchored_homography_beats_bad_corners():
    """Bad baseline-corner estimates + exact kitchen corners must fix kitchen zone."""
    # ground truth camera: simple projective map built from exact correspondences
    exact_corners = [[400.0, 200.0], [880.0, 200.0], [1180.0, 680.0], [100.0, 680.0]]
    truth = CourtCalibration(exact_corners)  # defines the "true" camera

    def px_of(court_xy):  # invert truth homography to synthesize pixel observations
        H_inv = np.linalg.inv(truth.H)
        p = np.array([court_xy[0], court_xy[1], 1.0])
        q = H_inv @ p
        return [float(q[0] / q[2]), float(q[1] / q[2])]

    kitchen_px = [px_of((0, 15)), px_of((20, 15)), px_of((20, 29)), px_of((0, 29))]
    # user estimates baseline corners badly (30-40 px off, e.g. off-frame guesses)
    bad_corners = [[430.0, 170.0], [920.0, 175.0], [1150.0, 710.0], [140.0, 705.0]]

    player_at_kitchen_px = px_of((10, 16))  # truly standing 6 ft from the net
    bad_only = CourtCalibration(bad_corners)
    anchored = CourtCalibration(bad_corners, kitchen_px)
    err_bad = abs(bad_only.to_court(np.array([player_at_kitchen_px]))[0][1] - 16.0)
    err_anchored = abs(anchored.to_court(np.array([player_at_kitchen_px]))[0][1] - 16.0)
    assert err_anchored < err_bad, (err_anchored, err_bad)
    assert err_anchored < 1.0, f"anchored error {err_anchored:.2f} ft too large"


CAMERA_CORNERS = [[400.0, 200.0], [880.0, 200.0], [1180.0, 680.0], [100.0, 680.0]]


def test_pick_opponent_finds_far_side_player():
    calib = CourtCalibration(CAMERA_CORNERS)
    subject = _frag(1, 0, 200, 700, 600)     # near-bottom pixels: subject's baseline
    partner = _frag(2, 0, 200, 300, 600)     # same side as subject (doubles partner)
    opponent = _frag(3, 0, 200, 700, 100)    # far-top pixels: other side of the net
    spectator = _frag(4, 0, 10, 700, 100)    # opponent-side position but too few frames
    df = pd.concat([subject, partner, opponent, spectator], ignore_index=True)

    subject_ids = stitch_chain_ids(df, 1)
    subject_y = float(np.median(calib.to_court(feet_px(subject))[:, 1]))
    assert (subject_y - NET_Y) > 0  # sanity: subject really is on the "near" side

    opp = pick_opponent(df, calib, subject_ids, subject_y)
    assert opp == 3, opp


def test_pick_opponent_none_when_nobody_else_visible():
    calib = CourtCalibration(CAMERA_CORNERS)
    subject = _frag(1, 0, 200, 700, 600)
    subject_ids = stitch_chain_ids(subject, 1)
    subject_y = float(np.median(calib.to_court(feet_px(subject))[:, 1]))
    assert pick_opponent(subject, calib, subject_ids, subject_y) is None


if __name__ == "__main__":
    for fn in [test_stitch_follows_across_id_breaks, test_stitch_respects_gap_and_distance,
               test_kitchen_anchored_homography_beats_bad_corners,
               test_pick_opponent_finds_far_side_player,
               test_pick_opponent_none_when_nobody_else_visible]:
        fn()
        print(f"ok {fn.__name__}")
