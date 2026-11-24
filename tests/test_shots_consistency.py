"""Shot speed/type, serve and landing lookups: one consistent ground-plane story.

Screen-space ball speed is foreshortened by perspective (on the sample, 44 ft of
court length spans ~470 px vertically but 20 ft of width spans ~715 px: a ball
travelling along the court looks ~3.3x slower than one travelling across), and the
serve/landing lookups ignored where the ball landed relative to the server and let a
bounce belonging to the NEXT shot be credited to this one. Whatever the detector
glitched into an absurd speed was still given a shot type.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shots import opponent_shot_landings, serve_metrics, shot_report

FPS = 30.0
CORNERS = [[300.0, 150.0], [1000.0, 150.0], [1250.0, 700.0], [50.0, 700.0]]
NO_BOUNCES = pd.DataFrame(columns=["x", "y", "t"])


def _b(*rows):
    return pd.DataFrame(rows, columns=["x", "y", "t"])


def _pos(t0, y, x=10.0):
    return pd.DataFrame({"t": [t0 - 0.2, t0, t0 + 0.2], "x": x, "y": y})


# ── landings are capped at the next hit ─────────────────────────────────

def test_a_bounce_after_the_next_hit_is_not_credited_to_this_one():
    hit_times = np.array([1.0, 1.8])
    hitters = ["opponent1", "subject"]
    late = _b((10.0, 30.0, 2.1))                      # belongs to the subject's 1.8 s shot
    assert opponent_shot_landings(hit_times, hitters, late, "opponent1") == []
    early = _b((10.0, 30.0, 1.5))
    assert len(opponent_shot_landings(hit_times, hitters, early, "opponent1")) == 1


def test_a_landing_far_off_the_court_is_implausible():
    assert opponent_shot_landings(np.array([1.0]), ["opponent1"], _b((34.0, 30.0, 1.4)), "opponent1") == []
    assert len(opponent_shot_landings(np.array([1.0]), ["opponent1"], _b((24.0, 30.0, 1.4)), "opponent1")) == 1


# ── a serve must land on the receiver's side ────────────────────────────

RALLIES = [{"start": 1.0, "end": 3.0, "hits": 3, "duration": 2.0}]
HITS = np.array([1.0, 2.0, 3.0])
MINE = np.array([True, False, True])


def test_a_serve_that_lands_on_the_servers_own_side_is_not_a_measured_serve():
    out = serve_metrics(RALLIES, MINE, HITS, _b((10.0, 38.0, 1.4)), pos=_pos(1.0, y=40.0))
    assert out == {"serves_measured": 0}


def test_a_serve_that_lands_on_the_far_side_is_measured():
    out = serve_metrics(RALLIES, MINE, HITS, _b((10.0, 6.0, 1.4)), pos=_pos(1.0, y=40.0))
    assert out["serves_measured"] == 1 and out["serves"][0]["depth_ft"] == 6.0


def test_the_side_check_works_from_the_far_end_too():
    out = serve_metrics(RALLIES, MINE, HITS, _b((10.0, 38.0, 1.4)), pos=_pos(1.0, y=4.0))
    assert out["serves_measured"] == 1 and out["serves"][0]["depth_ft"] == 6.0


# ── hits outside every rally are not shots ──────────────────────────────

def _ball(frames_from, n=12, dx=10.0):
    f = np.arange(frames_from, frames_from + n)
    return pd.DataFrame({"frame": f, "x": 100.0 + dx * (f - frames_from), "y": 300.0, "seg": frames_from})


def test_an_isolated_hit_outside_a_rally_is_not_classified_as_a_shot():
    hits = np.array([1.0, 50.0])
    ball = pd.concat([_ball(30), _ball(1500)], ignore_index=True)
    pos = pd.DataFrame({"t": np.linspace(0, 60, 61), "x": 10.0, "y": 30.0})
    rep = shot_report(hits, np.array([True, True]), ball, {"coverage": 0.9, "frames": 1800}, pos,
                      [{"start": 1.0, "end": 1.0, "hits": 1, "duration": 0.0}], CORNERS, NO_BOUNCES, FPS)
    assert [s["t"] for s in rep["shots"]] == [1.0]


# ── ground-plane speed ──────────────────────────────────────────────────

def _one_shot(bounces, pos=None, ball=None):
    hits = np.array([1.0])
    ball = _ball(30) if ball is None else ball
    pos = _pos(1.0, y=40.0) if pos is None else pos
    return shot_report(hits, np.array([True]), ball, {"coverage": 0.9, "frames": 90}, pos,
                       [{"start": 1.0, "end": 1.0, "hits": 1, "duration": 0.0}], CORNERS, bounces, FPS)


def test_speed_uses_hitter_to_bounce_distance_over_time_when_the_bounce_is_known():
    rep = _one_shot(_b((10.0, 10.0, 2.0)))                # 30 ft in 1.0 s
    s = rep["shots"][0]
    assert s["mph_source"] == "ground"
    assert abs(s["mph"] - 30.0 / 1.46667) < 0.1
    assert s["type"] == "medium"                          # ~20 mph: between a dink and a drive


def test_a_fast_ground_shot_is_a_drive_and_a_slow_one_near_the_net_a_dink():
    assert _one_shot(_b((10.0, 10.0, 1.7)))["shots"][0]["type"] == "drive"          # 30 ft in 0.7 s = 29 mph
    dink = _one_shot(_b((10.0, 28.0, 2.5)), pos=_pos(1.0, y=24.0))["shots"][0]      # 4 ft in 1.5 s = 1.8 mph
    assert dink["mph_source"] == "ground" and dink["type"] == "dink"


def test_without_a_usable_bounce_it_falls_back_to_screen_speed_and_says_so():
    s = _one_shot(NO_BOUNCES)["shots"][0]
    assert s["mph_source"] == "screen" and s["mph"] is not None


def test_a_bounce_that_belongs_to_the_next_shot_is_not_used_for_speed():
    hits = np.array([1.0, 1.8])
    ball = _ball(30)
    rep = shot_report(hits, np.array([True, False]), ball, {"coverage": 0.9, "frames": 90}, _pos(1.0, y=40.0),
                      [{"start": 1.0, "end": 1.8, "hits": 2, "duration": 0.8}], CORNERS, _b((10.0, 10.0, 2.4)), FPS)
    assert rep["shots"][0]["mph_source"] == "screen"


def test_a_glitch_speed_gets_no_type():
    """An in-court 'bounce' 38 ft away only 0.3 s after the hit is ~86 mph: not a real
    flight. No speed and no type (it used to be labelled a drive)."""
    s = _one_shot(_b((10.0, 2.0, 1.3)))["shots"][0]
    assert s["mph"] is None and s["type"] == "unknown"


def test_a_bounce_far_off_the_court_is_filtered_before_it_can_become_a_speed():
    s = _one_shot(_b((10.0, -30.0, 1.3)))["shots"][0]
    assert s["mph_source"] == "screen"


def test_a_bounce_too_close_in_time_or_space_is_not_used_for_speed():
    s = _one_shot(_b((10.0, 39.0, 1.05)))["shots"][0]       # 1 ft, 50 ms: not a flight
    assert s["mph_source"] == "screen"
