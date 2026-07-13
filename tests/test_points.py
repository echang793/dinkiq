"""Point-outcome detection tests: hitter attribution, rally winners, summary."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from points import attribute_hitters, point_summary, rally_outcomes


def test_attribute_hitters_nearest_swing():
    hits = np.array([1.0, 2.0, 3.0])
    subject_swings = np.array([1.02, 3.05])
    opponent_swings = np.array([2.01])
    out = attribute_hitters(hits, subject_swings, opponent_swings)
    assert out == ["subject", "opponent", "subject"]


def test_attribute_hitters_unknown_when_no_swing_matches():
    hits = np.array([5.0])
    out = attribute_hitters(hits, np.array([1.0]), np.array([9.0]))
    assert out == ["unknown"]


def test_rally_outcomes_without_ball_assumes_last_hitter_wins():
    rallies = [{"start": 1.0, "end": 3.0, "hits": 3, "duration": 2.0}]
    hits = np.array([1.0, 2.0, 3.0])
    hitters = ["subject", "opponent", "subject"]  # subject served, subject hit last
    out = rally_outcomes(rallies, hits, hitters, bounces_court=None)
    assert out == [{"server": "subject", "winner": "subject", "unforced_error": False}]


def test_rally_outcomes_ball_out_flips_winner_to_unforced_error():
    rallies = [{"start": 1.0, "end": 3.0, "hits": 3, "duration": 2.0}]
    hits = np.array([1.0, 2.0, 3.0])
    hitters = ["subject", "opponent", "subject"]  # subject hit last...
    # ...but it bounced well outside the court -> subject's own error, opponent wins
    bounces = pd.DataFrame({"x": [50.0], "y": [22.0], "t": [3.3]})
    out = rally_outcomes(rallies, hits, hitters, bounces_court=bounces)
    assert out[0]["winner"] == "opponent"
    assert out[0]["unforced_error"] is True


def test_rally_outcomes_ball_in_keeps_hitter_as_winner():
    rallies = [{"start": 1.0, "end": 3.0, "hits": 3, "duration": 2.0}]
    hits = np.array([1.0, 2.0, 3.0])
    hitters = ["subject", "opponent", "subject"]
    bounces = pd.DataFrame({"x": [10.0], "y": [40.0], "t": [3.3]})  # well inbounds
    out = rally_outcomes(rallies, hits, hitters, bounces_court=bounces)
    assert out[0] == {"server": "subject", "winner": "subject", "unforced_error": False}


def test_rally_outcomes_unknown_last_hitter():
    rallies = [{"start": 1.0, "end": 3.0, "hits": 2, "duration": 2.0}]
    hits = np.array([1.0, 3.0])
    hitters = ["subject", "unknown"]
    out = rally_outcomes(rallies, hits, hitters)
    assert out == [{"server": "subject", "winner": "unknown", "unforced_error": None}]


def test_point_summary_counts_and_rates():
    outcomes = [
        {"server": "subject", "winner": "subject", "unforced_error": False},
        {"server": "subject", "winner": "opponent", "unforced_error": True},
        {"server": "opponent", "winner": "subject", "unforced_error": False},
        {"server": "opponent", "winner": "unknown", "unforced_error": None},
    ]
    s = point_summary(outcomes, ball_available=True)
    assert s["points_scored"] == 3  # unknown excluded
    assert s["points_won"] == 2 and s["points_lost"] == 1
    assert s["win_pct"] == 66.7
    assert s["serve_win_pct"] == 50.0   # 1 of 2 subject-served points won
    assert s["return_win_pct"] == 100.0  # 1 of 1 subject-returned point won
    assert s["unforced_errors"] == 1
    assert "caveat" not in s


def test_point_summary_no_ball_adds_caveat():
    outcomes = [{"server": "subject", "winner": "subject", "unforced_error": False}]
    s = point_summary(outcomes, ball_available=False)
    assert "caveat" in s


def test_point_summary_empty():
    s = point_summary([], ball_available=True)
    assert s["points_scored"] == 0
    assert s["win_pct"] is None
    assert s["serve_win_pct"] is None


if __name__ == "__main__":
    for fn in [test_attribute_hitters_nearest_swing, test_attribute_hitters_unknown_when_no_swing_matches,
               test_rally_outcomes_without_ball_assumes_last_hitter_wins,
               test_rally_outcomes_ball_out_flips_winner_to_unforced_error,
               test_rally_outcomes_ball_in_keeps_hitter_as_winner,
               test_rally_outcomes_unknown_last_hitter,
               test_point_summary_counts_and_rates, test_point_summary_no_ball_adds_caveat,
               test_point_summary_empty]:
        fn()
        print(f"ok {fn.__name__}")
