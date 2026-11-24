"""Who is who: subject/partner picking, exclusive stitching, opponent chains.

On the sample session the partner click was 2.3 body-heights from ANY detection
and pick_subject still returned the nearest track -- the subject's own -- so
subject == partner, which silently corrupted every doubles statistic (82 swings
each, partner never attributed a hit). pick_subject had no distance limit, no
way to exclude an already-chosen player, and measured "nearest over 90 frames",
so a passer-by crossing the clicked spot could win.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import tracking
from court import NET_Y, CourtCalibration
from tracking import (PlayerNotFound, TrackIndex, pick_opponent_chains, pick_opponents,
                      pick_subject, stitch_subject)

CAMERA_CORNERS = [[400.0, 200.0], [880.0, 200.0], [1180.0, 680.0], [100.0, 680.0]]


def _person(tid, f0, n, cx, cy, h=160.0, dx=0.0, dy=0.0):
    """A person whose bbox centre is (cx, cy) at f0, drifting (dx, dy) per frame."""
    frames = np.arange(f0, f0 + n)
    x = cx + dx * (frames - f0)
    y = cy + dy * (frames - f0)
    w = h * 0.4
    return pd.DataFrame({"frame": frames, "track_id": tid, "x1": x - w / 2, "y1": y - h / 2,
                         "x2": x + w / 2, "y2": y + h / 2, "conf": 0.9,
                         "lwx": 0.0, "lwy": 0.0, "rwx": 0.0, "rwy": 0.0})


def _df(*people):
    return pd.concat(people, ignore_index=True)


# ── pick_subject ────────────────────────────────────────────────────────

def test_a_click_on_a_person_picks_them():
    df = _df(_person(1, 0, 60, 300, 400), _person(2, 0, 60, 800, 400))
    assert pick_subject(df, (305, 420)) == 1
    assert pick_subject(df, (810, 380)) == 2


def test_a_click_nowhere_near_anyone_raises_instead_of_guessing():
    df = _df(_person(1, 0, 60, 300, 400))
    with pytest.raises(PlayerNotFound, match="tap"):
        pick_subject(df, (900, 150))     # ~4 body-heights from the only person


def test_the_person_standing_there_at_the_start_beats_a_passer_by_later():
    df = _df(_person(1, 0, 90, 300, 400),                       # was there from frame 0
             _person(2, 40, 40, 300, 400))                      # walks through the same spot at frame 40
    assert pick_subject(df, (300, 400)) == 1


def test_earliest_detection_wins_even_if_a_later_one_is_closer():
    df = _df(_person(1, 0, 90, 330, 400),            # a little off the click, but there at frame 0
             _person(2, 30, 40, 300, 400))           # exactly on the click, 1 s later
    assert pick_subject(df, (300, 400)) == 1


def test_overlapping_boxes_resolve_to_the_nearest_centre():
    df = _df(_person(1, 0, 60, 300, 400, h=200), _person(2, 0, 60, 340, 400, h=200))
    assert pick_subject(df, (330, 400)) == 2


def test_excluded_ids_cannot_be_picked_so_partner_never_equals_subject():
    df = _df(_person(1, 0, 60, 300, 400))
    with pytest.raises(PlayerNotFound):
        pick_subject(df, (305, 400), exclude_ids={1})


def test_exclusion_picks_the_next_best_real_person():
    df = _df(_person(1, 0, 60, 300, 400, h=200), _person(2, 0, 60, 360, 400, h=200))
    assert pick_subject(df, (310, 400)) == 1
    assert pick_subject(df, (310, 400), exclude_ids={1}) == 2


def test_a_player_not_yet_detected_in_the_first_seconds_is_found_in_the_wider_window():
    df = _df(_person(1, 0, 400, 300, 400), _person(2, 150, 200, 800, 400))   # appears at 5 s
    assert pick_subject(df, (800, 400)) == 2


def test_a_player_who_never_appears_near_the_click_is_not_found_even_in_the_wider_window():
    df = _df(_person(1, 0, 400, 300, 400), _person(2, 450, 100, 800, 400))   # appears at 15 s
    with pytest.raises(PlayerNotFound):
        pick_subject(df, (800, 400))


def test_empty_tracks_still_raise_value_error_as_before():
    with pytest.raises(ValueError):
        pick_subject(pd.DataFrame(columns=tracking.COLUMNS), (1, 1))
    assert issubclass(PlayerNotFound, ValueError)


# ── exclusive stitching ─────────────────────────────────────────────────

def test_two_players_cannot_both_claim_the_same_fragment():
    a = _person(1, 0, 50, 100, 300, dx=2)               # ends at x=198
    b = _person(2, 0, 50, 110, 300, dx=2)               # ends at x=208, right beside A
    ambiguous = _person(3, 55, 50, 215, 300, dx=2)      # either of them could have become this
    df = _df(a, b, ambiguous)
    chain_a = set(stitch_subject(df, 1)["track_id"])
    assert 3 in chain_a
    chain_b = set(stitch_subject(df, 2, exclude_ids=chain_a)["track_id"])
    assert chain_b == {2}, "B must not take the fragment A already owns"
    assert not (chain_a & chain_b)


def test_stitch_subject_never_returns_excluded_ids_even_as_the_seed_neighbourhood():
    df = _df(_person(1, 0, 50, 100, 300, dx=2), _person(2, 55, 50, 215, 300, dx=2))
    assert set(stitch_subject(df, 1, exclude_ids={2})["track_id"]) == {1}


def test_index_gives_the_same_chain_as_the_one_shot_function():
    df = _df(_person(1, 0, 50, 100, 300, dx=2), _person(7, 55, 50, 210, 300, dx=2),
             _person(9, 110, 50, 320, 300, dx=2), _person(4, 0, 160, 900, 100))
    idx = TrackIndex(df)
    assert idx.chain_ids(1) == [1, 7, 9]
    assert set(stitch_subject(df, 1)["track_id"]) == set(idx.chain_ids(1))
    assert set(stitch_subject(df, 1, index=idx)["track_id"]) == {1, 7, 9}


def test_index_frame_is_ordered_and_deduplicated_by_frame():
    df = _df(_person(1, 0, 60, 100, 300), _person(2, 50, 40, 100, 300))   # overlapping fragments
    out = TrackIndex(df).frame_for([1, 2])
    assert out["frame"].is_monotonic_increasing and out["frame"].is_unique
    assert len(out) == 90


# ── opponents: chains + per-frame side ──────────────────────────────────

def _court_y_px(calib, court_y):
    """A pixel row whose feet project to the given court y (x = court centre)."""
    inv = np.linalg.inv(calib.H)
    p = inv @ np.array([10.0, court_y, 1.0])
    return p[0] / p[2], p[1] / p[2]


def test_one_opponent_fragmented_into_ids_is_one_chain_not_two_opponents():
    """Doubles asks for 2 opponents; three fragments of ONE person used to fill both slots."""
    calib = CourtCalibration(CAMERA_CORNERS)
    sx, sy = _court_y_px(calib, 38.0)      # subject's side (near)
    ox, oy = _court_y_px(calib, 6.0)       # far side
    subject = _person(1, 0, 300, sx, sy - 80)
    # the same far-side player, re-id'd twice with small gaps, walking right
    f1 = _person(10, 0, 100, ox, oy - 60, dx=1.0)
    f2 = _person(11, 105, 100, ox + 105, oy - 60, dx=1.0)
    f3 = _person(12, 210, 90, ox + 210, oy - 60, dx=1.0)
    df = _df(subject, f1, f2, f3)
    chains = pick_opponent_chains(df, calib, exclude_ids={1}, subject_pos=None,
                                  subject_median_y=38.0, n=2)
    assert len(chains) == 1 and set(chains[0]) == {10, 11, 12}
    assert pick_opponents(df, calib, {1}, 38.0, n=2) == [10]   # the id list stays one-per-person


def test_sides_are_compared_per_frame_so_an_end_change_does_not_confuse_the_opponent():
    """Players switch ends mid-session. Against a single session-median side the
    opponent scores ~50% 'opposite'; compared frame by frame it is 100%."""
    calib = CourtCalibration(CAMERA_CORNERS)
    near = _court_y_px(calib, 38.0)
    far = _court_y_px(calib, 6.0)
    n = 200
    # frames 0-99: subject near, opponent far. frames 100-199: they swap ends.
    subject = _df(_person(1, 0, 100, near[0], near[1] - 80), _person(1, 100, 100, far[0], far[1] - 60))
    opp = _df(_person(2, 0, 100, far[0] + 200, far[1] - 60), _person(2, 100, 100, near[0] + 200, near[1] - 80))
    # a bystander who stays on the subject's STARTING side the whole time
    bystander = _person(3, 0, n, near[0] - 200, near[1] - 80)
    df = _df(subject, opp, bystander)
    pos = pd.DataFrame({"frame": np.arange(n), "y": np.where(np.arange(n) < 100, 38.0, 6.0)})
    chains = pick_opponent_chains(df, calib, exclude_ids={1}, subject_pos=pos,
                                  subject_median_y=float(np.median(pos["y"])), n=1)
    assert chains == [[2]], chains


def test_opponent_pick_without_subject_positions_falls_back_to_the_session_median_side():
    calib = CourtCalibration(CAMERA_CORNERS)
    near = _court_y_px(calib, 38.0)
    far = _court_y_px(calib, 6.0)
    df = _df(_person(1, 0, 200, near[0], near[1] - 80), _person(3, 0, 200, far[0], far[1] - 60))
    assert pick_opponents(df, calib, {1}, 38.0, n=1) == [3]
    assert NET_Y == 22.0   # (the fixture's 38 / 6 are on opposite sides of this)
