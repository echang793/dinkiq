"""Swing detection from pose: wrist motion RELATIVE to the body, in body-heights/s.

Measured on the sample (4 players, 71 audio pops): the old definition (absolute
wrist px/s over court width) flagged 36-45% of frames for the opponents, and its
swing peaks coincided with audio hits LESS often than random times do
(lift 0.87). A jogging player's wrists move fast in the image, so every run was a
"swing"; and a far-court player (small in frame) moved fewer pixels for the same
swing. Relative-to-body motion over the player's own height fixes both.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from events import (PLAYER_HEIGHT_FT, SWING_MIN_BODY_HEIGHTS_S, detect_swings, wrist_speed)
from shots import swing_speed_at

FPS = 30.0
STEP = 2  # tracking stride: samples are 2 frames apart (15 Hz)


def _chain(n, h=160.0, body_dx=0.0, wrist_rel=None, track_ids=None):
    """A player `h` px tall whose body centre drifts body_dx px/sample. wrist_rel(i) -> the
    left wrist's (x, y) offset from the body centre, in body-heights; right wrist undetected."""
    i = np.arange(n)
    cx = 400.0 + body_dx * i
    cy = np.full(n, 360.0)
    w = h * 0.4
    off = np.array([wrist_rel(k) for k in i]) if wrist_rel else np.tile([0.3, 0.0], (n, 1))
    return pd.DataFrame({
        "frame": i * STEP, "track_id": 1 if track_ids is None else track_ids,
        "x1": cx - w / 2, "x2": cx + w / 2, "y1": cy - h / 2, "y2": cy + h / 2, "conf": 0.9,
        "lwx": cx + off[:, 0] * h, "lwy": cy + off[:, 1] * h, "rwx": 0.0, "rwy": 0.0})


def _whip(at, speed_bh_s, samples=3, h=160.0):
    """wrist_rel(i): rests at (0.3, 0), then sweeps `speed_bh_s` body-heights/s for `samples` samples from `at`."""
    step = speed_bh_s * (STEP / FPS)   # body-heights moved per sample
    def rel(i):
        if i < at:
            return (0.3, 0.0)
        return (0.3 + step * min(i - at, samples), 0.0)
    return rel


def test_a_player_who_just_walks_or_runs_does_not_swing():
    """Body moves 12 px per sample (= 90 px/s: a brisk walk) with the wrist locked to it.
    The old absolute-wrist definition counted this."""
    chain = _chain(150, body_dx=12.0)
    ws = wrist_speed(chain, FPS)
    assert np.nanmax(ws["speed"]) < 0.1
    assert len(detect_swings(ws)) == 0


def test_a_real_whip_of_the_wrist_is_one_swing_at_the_right_time():
    chain = _chain(150, wrist_rel=_whip(at=60, speed_bh_s=4.0))
    swings = detect_swings(wrist_speed(chain, FPS))
    assert len(swings) == 1
    assert abs(swings[0] - (60 * STEP / FPS)) < 0.25, swings


def test_a_gentle_wrist_motion_is_below_the_threshold():
    chain = _chain(150, wrist_rel=_whip(at=60, speed_bh_s=SWING_MIN_BODY_HEIGHTS_S * 0.5))
    assert len(detect_swings(wrist_speed(chain, FPS))) == 0


def test_the_same_swing_is_detected_for_a_near_and_a_far_player():
    """Same motion in body units, one player 2.5x smaller in the image (far court)."""
    near = detect_swings(wrist_speed(_chain(150, h=250.0, wrist_rel=_whip(60, 4.0)), FPS))
    far = detect_swings(wrist_speed(_chain(150, h=100.0, wrist_rel=_whip(60, 4.0)), FPS))
    assert len(near) == len(far) == 1 and abs(near[0] - far[0]) < 1e-9


def test_a_swing_while_the_body_is_running_is_still_found():
    chain = _chain(150, body_dx=12.0, wrist_rel=_whip(at=60, speed_bh_s=4.0))
    assert len(detect_swings(wrist_speed(chain, FPS))) == 1


def test_the_jump_where_one_track_id_hands_over_to_another_is_not_a_swing():
    """Stitched fragments are different detections: the bbox centre and wrist can jump
    between them without any real motion."""
    a, b = _chain(40), _chain(40)
    b = b.assign(frame=b["frame"] + 80, track_id=2, lwx=b["lwx"] + 90.0, lwy=b["lwy"] - 70.0)
    ws = wrist_speed(pd.concat([a, b], ignore_index=True), FPS)
    assert len(detect_swings(ws)) == 0
    assert np.nanmax(ws["speed"]) < 0.5


def test_undetected_wrists_are_masked_not_treated_as_motion():
    chain = _chain(150)
    chain.loc[60:64, ["lwx", "lwy"]] = 0.0     # wrist lost for five samples
    ws = wrist_speed(chain, FPS)
    assert len(detect_swings(ws)) == 0
    assert ws["speed"].iloc[60:66].isna().sum() >= 4


def test_a_long_gap_between_samples_is_not_a_fast_move():
    chain = _chain(100)
    chain["frame"] = np.where(np.arange(100) >= 50, chain["frame"] + 300, chain["frame"])  # 10 s gap
    chain.loc[50:, ["lwx"]] += 200.0
    assert len(detect_swings(wrist_speed(chain, FPS))) == 0


def test_the_wrist_speed_signature_no_longer_depends_on_the_court():
    """Normalization is by the player's own height; there is no court-width argument."""
    assert list(wrist_speed(_chain(10), FPS).columns) == ["t", "speed"]


def test_swing_mph_is_the_peak_in_the_window_converted_with_body_height():
    ws = pd.DataFrame({"t": [0.85, 0.95, 1.0, 1.05, 1.5], "speed": [1.0, 2.0, 4.0, 2.5, 9.0]})
    expect = round(4.0 * PLAYER_HEIGHT_FT / 1.46667, 1)
    assert swing_speed_at(ws, 1.0) == expect          # peak (4.0), not the median, and not the 9.0 outside the window
    assert swing_speed_at(ws, 100.0) is None


def test_an_implausible_swing_speed_is_dropped_not_reported():
    ws = pd.DataFrame({"t": [1.0], "speed": [40.0]})  # ~150 mph wrist: a tracking glitch
    assert swing_speed_at(ws, 1.0) is None
