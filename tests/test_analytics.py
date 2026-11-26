"""Analytics derived from artifacts the pipeline already writes: no new tracking.

Everything is gated on a sample size and reports it, because a percentage over two points is
noise. Nothing here may need a ball track unless it says so -- the sample footage has none.
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient

import analytics
import server
from pipeline import SESSIONS
from points import rally_outcomes

client = TestClient(server.app)


def _o(server_, winner, ue=None, kind=None, last="subject"):
    row = {"server": server_, "last_hitter": last, "winner": winner, "unforced_error": ue}
    if kind:
        row["error_kind"] = kind
    return row


RALLIES = [
    {"start": 10.0, "end": 20.0, "hits": 8, "duration": 10.0},
    {"start": 30.0, "end": 33.0, "hits": 2, "duration": 3.0},
    {"start": 50.0, "end": 52.0, "hits": 3, "duration": 2.0},
    {"start": 70.0, "end": 72.0, "hits": 3, "duration": 2.0},
    {"start": 90.0, "end": 100.0, "hits": 7, "duration": 10.0},
]
OUTCOMES = [
    _o("subject", "my_team", False),
    _o("opponent1", "opp_team", True, "net"),
    _o("opponent1", "opp_team", True, "long"),
    _o("subject", "opp_team", True, "net"),
    _o("opponent1", "my_team", False, last="opponent1"),
]
EVENTS = {"rallies": RALLIES, "rally_count": 5}
POINTS = {"outcomes": OUTCOMES}
HITS = [
    {"t": 10.0, "hitter": "subject", "rally": 0, "n": 0},
    {"t": 11.0, "hitter": "opponent1", "rally": 0, "n": 1},
    {"t": 12.0, "hitter": "subject", "rally": 0, "n": 2},
    {"t": 30.0, "hitter": "opponent1", "rally": 1, "n": 0},
    {"t": 31.5, "hitter": "subject", "rally": 1, "n": 1},
    {"t": 50.0, "hitter": "opponent1", "rally": 2, "n": 0},
    {"t": 51.5, "hitter": "subject", "rally": 2, "n": 1},
    {"t": 70.0, "hitter": "subject", "rally": 3, "n": 0},
    {"t": 71.0, "hitter": "opponent1", "rally": 3, "n": 1},
    {"t": 71.5, "hitter": "subject", "rally": 3, "n": 2},
    {"t": 90.0, "hitter": "opponent1", "rally": 4, "n": 0},
    {"t": 91.5, "hitter": "subject", "rally": 4, "n": 1},
]
SHOTS = {"available": True,
         "shots": [{"t": 12.0, "type": "drive", "mph": 30.0}, {"t": 71.5, "type": "drop", "mph": 12.0},
                   {"t": 31.5, "type": "dink", "mph": 5.0}],
         "serves": [{"t": 10.0, "rally_index": 0, "side": "left"}]}


def _positions(returns=(31.5, 51.5, 91.5), secs_to_kitchen=2.0):
    """Subject starts in no-man's land (14 ft from the net), and 2 s after each return is
    at the kitchen line (7 ft from the net) for a few seconds."""
    t = np.arange(0.0, 110.0, 0.1)
    y = np.full_like(t, 8.0)
    for r in returns:
        y[(t >= r + secs_to_kitchen) & (t <= r + 6.0)] = 15.0
    return pd.DataFrame({"frame": (t * 30).astype(int), "t": t, "x": 10.0, "y": y})


# ── win rate by rally length ────────────────────────────────────────────

def test_win_rate_is_split_by_how_long_the_rally_went():
    b = {x["key"]: x for x in analytics.extras(EVENTS, HITS, POINTS, SHOTS, None)["by_rally_length"]["buckets"]}
    assert (b["short"]["played"], b["short"]["won"]) == (3, 0)
    assert b["medium"]["played"] == 0 and b["medium"]["win_pct"] is None
    assert (b["long"]["played"], b["long"]["won"], b["long"]["win_pct"]) == (2, 2, 100.0)


def test_unscored_rallies_are_left_out_of_the_win_rate_not_counted_as_losses():
    outcomes = [_o("subject", "unknown")] * 5
    r = analytics.extras(EVENTS, HITS, {"outcomes": outcomes}, SHOTS, None)["by_rally_length"]
    assert r["available"] is False and r["scored"] == 0


def test_too_few_scored_rallies_is_withheld():
    r = analytics.extras(EVENTS, HITS, {"outcomes": OUTCOMES[:2] + [_o("x", "unknown")] * 3}, SHOTS, None)
    assert r["by_rally_length"]["available"] is False


# ── pace ────────────────────────────────────────────────────────────────

def test_pace_reports_rallies_per_minute_and_work_to_rest():
    p = analytics.extras(EVENTS, HITS, POINTS, SHOTS, None)["pace"]
    assert p["available"] is True
    assert p["rallies_per_min"] == pytest.approx(5 / (90 / 60), abs=0.01)
    assert p["avg_rest_s"] == pytest.approx(15.75, abs=0.01)         # mean of 10, 17, 18, 18
    assert p["work_rest_ratio"] == pytest.approx(27 / 63, abs=0.01)


def test_a_long_break_between_games_is_not_counted_as_rest():
    ev = {"rallies": RALLIES[:3] + [{"start": 1000.0, "end": 1003.0, "hits": 3, "duration": 3.0}]}
    p = analytics.extras(ev, HITS, POINTS, SHOTS, None)["pace"]
    assert p["breaks"] == 1 and p["avg_rest_s"] == pytest.approx(13.5, abs=0.01)


def test_pace_needs_at_least_three_rallies():
    assert analytics.extras({"rallies": RALLIES[:2]}, HITS, POINTS, SHOTS, None)["pace"]["available"] is False


# ── return and run ──────────────────────────────────────────────────────

def test_return_and_run_times_how_long_you_take_to_reach_the_kitchen_after_returning():
    r = analytics.extras(EVENTS, HITS, POINTS, SHOTS, _positions())["return_and_run"]
    assert r["available"] is True and r["returns"] == 3 and r["reached"] == 3
    assert r["avg_secs_to_kitchen"] == pytest.approx(2.0, abs=0.15)


def test_a_return_you_never_followed_in_counts_as_not_reached():
    r = analytics.extras(EVENTS, HITS, POINTS, SHOTS, _positions(returns=(31.5,)))["return_and_run"]
    assert r["returns"] == 3 and r["reached"] == 1 and r["reached_pct"] == pytest.approx(33.3, abs=0.1)


def test_return_and_run_needs_positions():
    assert analytics.extras(EVENTS, HITS, POINTS, SHOTS, None)["return_and_run"]["available"] is False


# ── third shot ──────────────────────────────────────────────────────────

def test_third_shot_profile_counts_what_you_hit_after_your_own_serve():
    t = analytics.extras(EVENTS, HITS, POINTS, SHOTS, None)["third_shot"]
    assert t["available"] is True and t["n"] == 2 and t["mix"] == {"drive": 1, "drop": 1}


def test_third_shot_withheld_without_shot_types():
    t = analytics.extras(EVENTS, HITS, POINTS, {"available": False}, None)["third_shot"]
    assert t["available"] is False


# ── errors ──────────────────────────────────────────────────────────────

def test_errors_are_split_into_net_long_and_wide_for_your_side_only():
    e = analytics.extras(EVENTS, HITS, POINTS, SHOTS, None)["errors"]
    assert e["total"] == 3 and e["net"] == 2 and e["long"] == 1 and e["wide"] == 0


def test_the_opponents_errors_are_not_counted_as_yours():
    outs = [_o("subject", "my_team", True, "net", last="opponent1")] * 4
    assert analytics.extras(EVENTS, HITS, {"outcomes": outs}, SHOTS, None)["errors"]["total"] == 0


def test_a_dominant_error_kind_becomes_a_coaching_tip_with_its_sample_size():
    tips = analytics.extras(EVENTS, HITS, POINTS, SHOTS, None)["tips"]
    assert any("net" in t and "2 of 3" in t for t in tips)


def test_no_tip_from_a_tiny_sample():
    tips = analytics.extras(EVENTS, HITS, {"outcomes": OUTCOMES[:2]}, SHOTS, None)["tips"]
    assert not any("net" in t for t in tips)


# ── where the point ended ───────────────────────────────────────────────

def test_win_rate_by_where_you_stood_when_the_point_ended():
    z = analytics.extras(EVENTS, HITS, POINTS, SHOTS, _positions())["end_zone"]
    assert z["available"] is True
    assert sum(v["played"] for v in z["zones"].values()) == 5


# ── error direction is recorded where the outcome is decided ────────────

def _b(x, y, t):
    return pd.DataFrame({"x": [x], "y": [y], "t": [t]})


R1 = [{"start": 1.0, "end": 3.0, "hits": 3, "duration": 2.0}]
H3 = np.array([1.0, 2.0, 3.0])


@pytest.mark.parametrize("bx, by, kind", [(10.0, -3.0, "long"), (24.0, 8.0, "wide"), (10.0, 30.0, "net")])
def test_a_lost_point_records_whether_the_ball_went_long_wide_or_into_the_net(bx, by, kind):
    out = rally_outcomes(R1, H3, ["subject", "opponent1", "subject"], bounces_court=_b(bx, by, 3.3),
                         my_side_by_rally=[1])[0]
    assert out["unforced_error"] is True and out["error_kind"] == kind


def test_a_won_point_has_no_error_kind():
    out = rally_outcomes(R1, H3, ["subject", "opponent1", "subject"], bounces_court=_b(10.0, 8.0, 3.3),
                         my_side_by_rally=[1])[0]
    assert "error_kind" not in out


# ── moments: the clips behind a number ──────────────────────────────────

def test_moments_for_unforced_errors_match_the_count_and_point_at_the_rally():
    m = analytics.moments(EVENTS, POINTS, SHOTS)
    ue = m["unforced_errors"]
    assert len(ue) == 3 and [x["rally"] for x in ue] == [1, 2, 3]
    assert ue[0]["start"] < RALLIES[1]["start"] and ue[0]["end"] > RALLIES[1]["end"]


def test_moments_won_and_lost_partition_the_scored_rallies():
    m = analytics.moments(EVENTS, POINTS, SHOTS)
    assert [x["rally"] for x in m["won"]] == [0, 4] and [x["rally"] for x in m["lost"]] == [1, 2, 3]


def test_shot_moments_are_grouped_by_type_and_know_their_rally():
    m = analytics.moments(EVENTS, POINTS, SHOTS)
    assert [x["t"] for x in m["drive"]] == [12.0] and m["drive"][0]["rally"] == 0
    assert len(m["dink"]) == 1 and len(m["drop"]) == 1


def test_moments_window_never_starts_before_the_video_does():
    ev = {"rallies": [{"start": 0.5, "end": 2.0, "hits": 2, "duration": 1.5}]}
    m = analytics.moments(ev, {"outcomes": [_o("subject", "my_team", False)]}, {})
    assert m["won"][0]["start"] == 0.0


def test_the_longest_rally_is_a_moment():
    assert analytics.moments(EVENTS, POINTS, SHOTS)["longest"][0]["rally"] == 0


def test_moments_survive_missing_artifacts():
    assert analytics.moments({}, {}, {}) == {k: [] for k in analytics.MOMENT_KINDS}


# ── endpoints ───────────────────────────────────────────────────────────

def _done_session(sid="an"):
    d = SESSIONS / sid
    d.mkdir(parents=True)
    (d / "status.json").write_text(json.dumps({"stage": "done", "state": "done"}))
    (d / "meta.json").write_text("{}")
    (d / "events.json").write_text(json.dumps(EVENTS))
    (d / "hits.json").write_text(json.dumps(HITS))
    (d / "points.json").write_text(json.dumps(POINTS))
    (d / "shots.json").write_text(json.dumps(SHOTS))
    _positions().to_parquet(d / "positions.parquet", index=False)
    return d


def test_analytics_endpoint_serves_the_computed_report():
    d = _done_session()
    body = client.get(f"/api/session/{d.name}/analytics").json()
    assert body["errors"]["total"] == 3 and body["return_and_run"]["returns"] == 3


def test_moments_endpoint_serves_the_clip_windows():
    d = _done_session()
    body = client.get(f"/api/session/{d.name}/moments").json()
    assert len(body["unforced_errors"]) == 3


@pytest.mark.parametrize("path", ["analytics", "moments"])
def test_endpoints_refuse_a_session_that_is_not_done(path):
    d = _done_session()
    (d / "status.json").write_text(json.dumps({"stage": "tracking", "state": "running"}))
    assert client.get(f"/api/session/{d.name}/{path}").status_code == 409


def test_analytics_endpoint_copes_with_a_session_missing_most_artifacts():
    d = SESSIONS / "bare"
    d.mkdir(parents=True)
    (d / "status.json").write_text(json.dumps({"stage": "done", "state": "done"}))
    body = client.get("/api/session/bare/analytics").json()
    assert body["pace"]["available"] is False and body["errors"]["total"] == 0


# ── the whole video, seekable ───────────────────────────────────────────

def test_the_full_video_supports_range_requests_for_seeking():
    d = _done_session("vid")
    (d / "video.mp4").write_bytes(bytes(range(256)) * 40)
    r = client.get("/api/session/vid/video", headers={"Range": "bytes=100-199"})
    assert r.status_code == 206 and len(r.content) == 100
    assert r.headers["content-range"].startswith("bytes 100-199/")


def test_video_404_before_it_exists():
    d = _done_session("novid")
    assert client.get(f"/api/session/{d.name}/video").status_code == 404
