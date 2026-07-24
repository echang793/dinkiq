"""Point-outcome detection tests: hitter attribution, rally winners, summary."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from points import (attribute_hitters, point_summary, rally_outcomes,
                    serve_side_win_rates, shot_type_outcomes)


def test_attribute_hitters_nearest_swing():
    hits = np.array([1.0, 2.0, 3.0])
    swings = {"subject": np.array([1.02, 3.05]), "opponent1": np.array([2.01])}
    out = attribute_hitters(hits, swings)
    assert out == ["subject", "opponent1", "subject"]


def test_attribute_hitters_unknown_when_no_swing_matches():
    hits = np.array([5.0])
    out = attribute_hitters(hits, {"subject": np.array([1.0]), "opponent1": np.array([9.0])})
    assert out == ["unknown"]


def test_attribute_hitters_doubles_four_way():
    hits = np.array([1.0, 2.0, 3.0, 4.0])
    swings = {"subject": np.array([1.02]), "partner": np.array([3.03]),
             "opponent1": np.array([2.01]), "opponent2": np.array([4.04])}
    out = attribute_hitters(hits, swings)
    assert out == ["subject", "opponent1", "partner", "opponent2"]


def test_rally_outcomes_without_ball_assumes_last_hitter_wins():
    rallies = [{"start": 1.0, "end": 3.0, "hits": 3, "duration": 2.0}]
    hits = np.array([1.0, 2.0, 3.0])
    hitters = ["subject", "opponent1", "subject"]  # subject served, subject hit last
    out = rally_outcomes(rallies, hits, hitters, bounces_court=None)
    assert out == [{"server": "subject", "last_hitter": "subject",
                   "winner": "subject", "unforced_error": False}]


def test_rally_outcomes_ball_out_flips_winner_to_unforced_error():
    rallies = [{"start": 1.0, "end": 3.0, "hits": 3, "duration": 2.0}]
    hits = np.array([1.0, 2.0, 3.0])
    hitters = ["subject", "opponent1", "subject"]  # subject hit last...
    # ...but it bounced well outside the court -> subject's own error, opp team wins
    bounces = pd.DataFrame({"x": [50.0], "y": [22.0], "t": [3.3]})
    out = rally_outcomes(rallies, hits, hitters, bounces_court=bounces)
    assert out[0]["winner"] == "opp_team"
    assert out[0]["unforced_error"] is True


def test_rally_outcomes_ball_in_keeps_hitter_as_winner():
    rallies = [{"start": 1.0, "end": 3.0, "hits": 3, "duration": 2.0}]
    hits = np.array([1.0, 2.0, 3.0])
    hitters = ["subject", "opponent1", "subject"]
    bounces = pd.DataFrame({"x": [10.0], "y": [40.0], "t": [3.3]})  # well inbounds
    out = rally_outcomes(rallies, hits, hitters, bounces_court=bounces)
    assert out[0] == {"server": "subject", "last_hitter": "subject",
                      "winner": "subject", "unforced_error": False}


def test_rally_outcomes_doubles_partner_hit_counts_as_my_team():
    rallies = [{"start": 1.0, "end": 3.0, "hits": 3, "duration": 2.0}]
    hits = np.array([1.0, 2.0, 3.0])
    hitters = ["subject", "opponent1", "partner"]  # partner hit last, on my team
    bounces = pd.DataFrame({"x": [10.0], "y": [40.0], "t": [3.3]})  # well inbounds
    out = rally_outcomes(rallies, hits, hitters, bounces_court=bounces)
    assert out[0] == {"server": "subject", "last_hitter": "partner",
                      "winner": "partner", "unforced_error": False}


def test_rally_outcomes_unknown_last_hitter():
    rallies = [{"start": 1.0, "end": 3.0, "hits": 2, "duration": 2.0}]
    hits = np.array([1.0, 3.0])
    hitters = ["subject", "unknown"]
    out = rally_outcomes(rallies, hits, hitters)
    assert out == [{"server": "subject", "last_hitter": "unknown",
                   "winner": "unknown", "unforced_error": None}]


def test_point_summary_counts_and_rates():
    outcomes = [
        {"server": "subject", "winner": "subject", "unforced_error": False},
        {"server": "subject", "winner": "opponent1", "unforced_error": True},
        {"server": "opponent1", "winner": "subject", "unforced_error": False},
        {"server": "opponent1", "winner": "unknown", "unforced_error": None},
    ]
    s = point_summary(outcomes, ball_available=True)
    assert s["points_scored"] == 3  # unknown excluded
    assert s["points_won"] == 2 and s["points_lost"] == 1
    assert s["win_pct"] == 66.7
    assert s["serve_win_pct"] == 50.0   # 1 of 2 subject-served points won
    assert s["return_win_pct"] == 100.0  # 1 of 1 subject-returned point won
    assert s["unforced_errors"] == 1
    assert "caveat" not in s


def test_point_summary_doubles_hits_by_player():
    outcomes = [{"server": "subject", "winner": "partner", "unforced_error": False}]
    hitters = ["subject", "opponent1", "partner", "opponent2", "subject"]
    s = point_summary(outcomes, ball_available=True, hitters=hitters)
    assert s["hits_by_player"] == {"subject": 2, "opponent1": 1, "partner": 1, "opponent2": 1}


def test_point_summary_no_ball_adds_caveat():
    outcomes = [{"server": "subject", "winner": "subject", "unforced_error": False}]
    s = point_summary(outcomes, ball_available=False)
    assert "caveat" in s


def test_point_summary_empty():
    s = point_summary([], ball_available=True)
    assert s["points_scored"] == 0
    assert s["win_pct"] is None
    assert s["serve_win_pct"] is None


def test_serve_side_win_rates_joins_by_rally_index():
    serves = [{"side": "middle", "rally_index": 0}, {"side": "left", "rally_index": 1},
             {"side": "middle", "rally_index": 2}]
    outcomes = [{"winner": "subject"}, {"winner": "opponent1"}, {"winner": "my_team"}]
    rates = serve_side_win_rates(serves, outcomes)
    assert rates == {"middle": {"attempts": 2, "win_pct": 100.0},
                     "left": {"attempts": 1, "win_pct": 0.0}}


def test_serve_side_win_rates_skips_unknown_and_out_of_range():
    serves = [{"side": "middle", "rally_index": 0}, {"side": "left", "rally_index": 5}]
    outcomes = [{"winner": "unknown"}]
    assert serve_side_win_rates(serves, outcomes) == {}


def test_shot_type_outcomes_joins_by_rally_end_time():
    # rally 0: subject's drive ended it, won. rally 1: subject's dink ended
    # it, lost. rally 2: opponent hit last, not subject -- excluded, since
    # the app doesn't classify opponent shot types.
    shots = [{"t": 3.0, "type": "drive"}, {"t": 21.0, "type": "dink"}]
    rallies = [{"start": 1.0, "end": 3.0}, {"start": 10.0, "end": 21.0},
              {"start": 30.0, "end": 33.0}]
    outcomes = [{"last_hitter": "subject", "winner": "subject"},
               {"last_hitter": "subject", "winner": "opponent1"},
               {"last_hitter": "opponent1", "winner": "opponent1"}]
    rates = shot_type_outcomes(shots, rallies, outcomes)
    assert rates == {"drive": {"attempts": 1, "win_pct": 100.0},
                     "dink": {"attempts": 1, "win_pct": 0.0}}


def test_shot_type_outcomes_empty_when_no_subject_ended_rallies():
    shots = [{"t": 3.0, "type": "drive"}]
    rallies = [{"start": 1.0, "end": 3.0}]
    outcomes = [{"last_hitter": "opponent1", "winner": "opponent1"}]
    assert shot_type_outcomes(shots, rallies, outcomes) == {}


if __name__ == "__main__":
    for fn in [test_attribute_hitters_nearest_swing, test_attribute_hitters_unknown_when_no_swing_matches,
               test_attribute_hitters_doubles_four_way,
               test_rally_outcomes_without_ball_assumes_last_hitter_wins,
               test_rally_outcomes_ball_out_flips_winner_to_unforced_error,
               test_rally_outcomes_ball_in_keeps_hitter_as_winner,
               test_rally_outcomes_doubles_partner_hit_counts_as_my_team,
               test_rally_outcomes_unknown_last_hitter,
               test_point_summary_counts_and_rates, test_point_summary_doubles_hits_by_player,
               test_point_summary_no_ball_adds_caveat,
               test_point_summary_empty,
               test_serve_side_win_rates_joins_by_rally_index,
               test_serve_side_win_rates_skips_unknown_and_out_of_range,
               test_shot_type_outcomes_joins_by_rally_end_time,
               test_shot_type_outcomes_empty_when_no_subject_ended_rallies]:
        fn()
        print(f"ok {fn.__name__}")
