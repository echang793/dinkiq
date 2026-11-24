"""Who won the point, and where did the ball go -- without inventing answers.

Without a ball track, points.py assumed the last hitter won. In recreational
pickleball most rallies end on an error, so the last hitter is usually the LOSER:
that assumption was systematically wrong, not just uncertain. And with a ball, a
shot into the net (bouncing on the hitter's own side, or never crossing) was credited
as a winner because only "out of the court" was checked.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from court import first_bounce_after
from points import point_summary, rally_outcomes

RALLY = [{"start": 1.0, "end": 3.0, "hits": 3, "duration": 2.0}]
HITS = np.array([1.0, 2.0, 3.0])
SUBJ_LAST = ["subject", "opponent1", "subject"]
OPP_LAST = ["opponent1", "subject", "opponent1"]
NEAR, FAR = 1, -1   # my side of the net is the near (y > 22) side in these fixtures


def _b(x, y, t):
    return pd.DataFrame({"x": [x], "y": [y], "t": [t]})


def _o(hitters, bounces, my_side=(NEAR,)):
    return rally_outcomes(RALLY, HITS, hitters, bounces_court=bounces, my_side_by_rally=list(my_side))[0]


# ── no ball: unknown, never a guess ─────────────────────────────────────

def test_without_a_ball_track_the_winner_is_unknown_not_the_last_hitter():
    out = rally_outcomes(RALLY, HITS, SUBJ_LAST, bounces_court=None)[0]
    assert out["winner"] == "unknown" and out["unforced_error"] is None
    assert out["server"] == "subject" and out["last_hitter"] == "subject"   # what IS known stays


def test_a_summary_with_no_ball_scores_no_points_and_says_why():
    outcomes = rally_outcomes(RALLY, HITS, SUBJ_LAST, bounces_court=None)
    s = point_summary(outcomes, ball_available=False)
    assert s["points_scored"] == 0 and s["win_pct"] is None
    assert "can't be determined" in s["caveat"] or "cannot be determined" in s["caveat"]


# ── with a ball: where it landed decides ────────────────────────────────

def test_a_ball_landing_in_on_the_other_side_wins_the_point_for_the_hitter():
    out = _o(SUBJ_LAST, _b(10.0, 8.0, 3.3))        # subject (near side) hit it into the far court
    assert out["winner"] == "my_team" and out["unforced_error"] is False


def test_a_ball_landing_out_beyond_the_baseline_is_the_hitters_error():
    out = _o(SUBJ_LAST, _b(10.0, -3.0, 3.3))       # 3 ft past the far baseline
    assert out["winner"] == "opp_team" and out["unforced_error"] is True


def test_a_ball_that_bounces_on_the_hitters_own_side_is_a_net_error():
    """It never crossed: the old code only tested 'out of the court' and called this a winner."""
    out = _o(SUBJ_LAST, _b(10.0, 30.0, 3.3))       # subject hit it, it bounced 8 ft past the net on THEIR side
    assert out["winner"] == "opp_team" and out["unforced_error"] is True


def test_the_same_net_error_is_symmetric_for_an_opponents_last_hit():
    out = _o(OPP_LAST, _b(10.0, 15.0, 3.3))        # opponent hit it, it bounced on the far (their) side
    assert out["winner"] == "my_team" and out["unforced_error"] is True


def test_an_opponent_hit_landing_in_on_my_side_is_their_winner():
    out = _o(OPP_LAST, _b(10.0, 35.0, 3.3))
    assert out["winner"] == "opp_team" and out["unforced_error"] is False


def test_a_bounce_right_at_the_net_line_is_too_close_to_call():
    out = _o(SUBJ_LAST, _b(10.0, 22.4, 3.3))       # within a foot of the net either way
    assert out["winner"] == "unknown"


def test_no_bounce_observed_after_the_last_hit_is_unknown_not_a_win():
    out = _o(SUBJ_LAST, pd.DataFrame({"x": [], "y": [], "t": []}))
    assert out["winner"] == "unknown" and out["unforced_error"] is None


def test_a_bounce_belonging_to_a_later_moment_is_not_this_rallys():
    out = _o(SUBJ_LAST, _b(10.0, 8.0, 3.0 + 3.0))  # 3 s after the last hit: outside the 2.5 s window
    assert out["winner"] == "unknown"


def test_a_glitch_bounce_far_off_court_is_ignored_not_called_out():
    out = _o(SUBJ_LAST, _b(10.0, -200.0, 3.3))
    assert out["winner"] == "unknown"


def test_without_a_known_side_only_the_in_out_rule_applies():
    """Side-of-net needs to know which end my team is on; without it, fall back to in/out."""
    out = rally_outcomes(RALLY, HITS, SUBJ_LAST, bounces_court=_b(10.0, 30.0, 3.3))[0]
    assert out["winner"] == "my_team"             # lands in court -> counted as in (can't see it's the wrong side)


def test_winners_are_always_team_labels_even_for_a_partner_or_second_opponent():
    for hitters, bounce in ((["subject", "opponent1", "partner"], _b(10.0, 8.0, 3.3)),
                            (["subject", "opponent2", "opponent2"], _b(10.0, 35.0, 3.3))):
        out = _o(hitters, bounce)
        assert out["winner"] in ("my_team", "opp_team"), out


def test_an_unattributed_last_hit_stays_unknown():
    out = _o(["subject", "opponent1", "unknown"], _b(10.0, 8.0, 3.3))
    assert out["winner"] == "unknown"


# ── the shared bounce lookup ────────────────────────────────────────────

def test_first_bounce_is_capped_at_the_next_hit():
    b = _b(10.0, 8.0, 11.4)
    assert first_bounce_after(b, 10.0, until=11.0) is None          # that bounce is the NEXT shot's
    assert first_bounce_after(b, 10.0, until=12.0) is not None


def test_first_bounce_rejects_a_bounce_far_off_court():
    assert first_bounce_after(_b(34.0, 8.0, 10.4), 10.0) is None    # 14 ft wide of the sideline
    assert first_bounce_after(_b(24.0, 8.0, 10.4), 10.0) is not None  # 4 ft wide: a real wide ball


def test_first_bounce_returns_the_earliest_plausible_one():
    b = pd.DataFrame({"x": [10.0, 10.0, 10.0], "y": [-200.0, 8.0, 9.0], "t": [10.1, 10.4, 10.6]})
    assert first_bounce_after(b, 10.0)["t"] == 10.4


@pytest.mark.parametrize("empty", [None, pd.DataFrame({"x": [], "y": [], "t": []})])
def test_no_bounces_gives_none(empty):
    assert first_bounce_after(empty, 10.0) is None
