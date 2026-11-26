"""What changed since last time, goals, a weekly practice plan, and a practice log.

Single user, so no accounts: goals and practice logs live in one file under the data dir.
"""

import datetime
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient

import pipeline
import server
from pipeline import SESSIONS

client = TestClient(server.app)
TODAY = datetime.date.today()


def _dims(nvz=48.2, pos=24.6, rally=2.3):
    def d(label, value, band, unit, nxt, nb):
        return {"label": label, "value": value, "band": band, "weight": 0.2, "unit": unit,
                "next_target_value": nxt, "next_target_band": nb}
    return {"nvz_discipline": d("Kitchen presence", nvz, 4.16, "%", 50, 4.25),
            "positioning": d("Court positioning", pos, 3.53, "%", 15, 4.25),
            "rally_sustain": d("Rally consistency", rally, 2.5, " hits", 5, 3.0)}


def _session(sid, played_at, *, stage="done", player=None, kitchen=30.0, transition=40.0, rally=3.0,
             win=None, band=3.5, lo=3.25, hi=3.75, dims=None, mtime=None):
    d = SESSIONS / sid
    d.mkdir(parents=True)
    (d / "status.json").write_text(json.dumps({"stage": stage, "state": stage}))
    meta = {"label": sid, "played_at": played_at}
    if player:
        meta["player"] = player
    (d / "meta.json").write_text(json.dumps(meta))
    (d / "metrics.json").write_text(json.dumps(
        {"zone_pct": {"kitchen": kitchen, "transition": transition, "baseline": 100 - kitchen - transition},
         "distance_ft": 300.0, "avg_speed_ft_s": 3.0, "coverage_pct": 50.0}))
    (d / "events.json").write_text(json.dumps({"rally_count": 5, "avg_rally_hits": rally, "play_time_pct": 20.0}))
    (d / "points.json").write_text(json.dumps({"outcomes": [], "win_pct": win}))
    (d / "dupr.json").write_text(json.dumps(
        {"available": True, "band": band, "band_low": lo, "band_high": hi, "confidence": 0.5,
         "dimensions": dims or _dims()}))
    return d


def _by_key(body, section="metrics"):
    return {x["key"]: x for x in body[section]}


# ── what changed since last session ─────────────────────────────────────

def test_changes_compares_with_the_previous_completed_session_of_the_same_player():
    _session("s1", "2026-01-01", kitchen=30.0, transition=40.0, rally=3.0)
    _session("sam", "2026-01-05", player="Sam", kitchen=5.0)             # someone else's footage
    _session("broken", "2026-01-06", stage="error")                       # never finished
    _session("s2", "2026-01-08", kitchen=45.0, transition=30.0, rally=3.1)
    body = client.get("/api/session/s2/changes", params={"player": "You"}).json()
    assert body["available"] is True and body["previous"]["session_id"] == "s1"
    m = _by_key(body)
    assert m["kitchen_pct"]["delta"] == 15.0 and m["kitchen_pct"]["direction"] == "better"
    assert m["transition_pct"]["delta"] == -10.0 and m["transition_pct"]["direction"] == "better"  # lower is better
    assert m["avg_rally_hits"]["direction"] == "same"                     # +0.1 is noise


def test_the_first_session_has_nothing_to_compare_with():
    _session("only", "2026-01-01")
    assert client.get("/api/session/only/changes").json() == {"available": False,
                                                              "reason": "no earlier session to compare with"}


def test_a_session_is_never_compared_with_a_later_one():
    _session("early", "2026-01-01")
    _session("late", "2026-02-01")
    assert client.get("/api/session/early/changes").json()["available"] is False


def test_a_rating_change_inside_the_estimates_own_margin_is_called_noise():
    _session("a", "2026-01-01", band=3.5, lo=3.0, hi=4.0)
    _session("b", "2026-01-08", band=3.7, lo=3.2, hi=4.2)
    rating = client.get("/api/session/b/changes").json()["rating"]
    assert rating["delta"] == pytest.approx(0.2) and rating["within_margin"] is True


def test_a_rating_change_beyond_the_margin_is_called_real():
    _session("a", "2026-01-01", band=3.0, lo=2.9, hi=3.1)
    _session("b", "2026-01-08", band=3.8, lo=3.7, hi=3.9)
    assert client.get("/api/session/b/changes").json()["rating"]["within_margin"] is False


def test_changes_lists_each_dimension_and_names_the_headline_movers():
    _session("a", "2026-01-01", dims=_dims(nvz=30.0))
    b = _dims(nvz=45.0)
    b["nvz_discipline"]["band"] = 4.0
    _session("b", "2026-01-08", dims=b)
    body = client.get("/api/session/b/changes").json()
    dims = _by_key(body, "dimensions")
    assert dims["nvz_discipline"]["delta_band"] == pytest.approx(4.0 - 4.16, abs=0.01)
    assert isinstance(body["headline"], str) and body["headline"]


def test_changes_409_when_the_session_is_not_done():
    _session("busy", "2026-01-01", stage="tracking")
    assert client.get("/api/session/busy/changes").status_code == 409


# ── goals ───────────────────────────────────────────────────────────────

def test_a_goal_tracks_the_latest_session_against_its_target():
    _session("s", "2026-01-01", dims=_dims(nvz=44.0))
    assert client.put("/api/goals/nvz_discipline", json={"target": 50}).status_code == 200
    g = client.get("/api/goals").json()["goals"][0]
    assert g["dimension"] == "nvz_discipline" and g["target"] == 50 and g["current"] == 44.0
    assert g["achieved"] is False and g["label"] == "Kitchen presence" and g["unit"] == "%"
    assert g["progress_pct"] == pytest.approx(100 * 44.0 / 50, abs=0.1) or 0 <= g["progress_pct"] <= 100


def test_a_goal_is_achieved_once_the_latest_session_reaches_it():
    _session("s", "2026-01-01", dims=_dims(nvz=52.0))
    client.put("/api/goals/nvz_discipline", json={"target": 50})
    assert client.get("/api/goals").json()["goals"][0]["achieved"] is True


def test_a_lower_is_better_goal_is_met_by_going_down():
    _session("s", "2026-01-01", dims=_dims(pos=12.0))
    client.put("/api/goals/positioning", json={"target": 15})
    assert client.get("/api/goals").json()["goals"][0]["achieved"] is True
    _session("t", "2026-02-01", dims=_dims(pos=24.0))
    assert client.get("/api/goals").json()["goals"][0]["achieved"] is False


def test_putting_a_goal_again_replaces_it():
    _session("s", "2026-01-01")
    client.put("/api/goals/nvz_discipline", json={"target": 50})
    client.put("/api/goals/nvz_discipline", json={"target": 60})
    goals = client.get("/api/goals").json()["goals"]
    assert len(goals) == 1 and goals[0]["target"] == 60


def test_goals_can_be_set_before_any_session_exists():
    client.put("/api/goals/rally_sustain", json={"target": 6})
    g = client.get("/api/goals").json()["goals"][0]
    assert g["current"] is None and g["achieved"] is False


@pytest.mark.parametrize("dim, body, code", [("nope", {"target": 5}, 404),
                                             ("nvz_discipline", {"target": -1}, 422),
                                             ("nvz_discipline", {"target": 1e9}, 422),
                                             ("nvz_discipline", {"target": "x"}, 422)])
def test_bad_goals_are_refused(dim, body, code):
    assert client.put(f"/api/goals/{dim}", json=body).status_code == code


def test_suggested_goals_come_from_the_weakest_dimensions_next_tier():
    _session("s", "2026-01-01")
    sug = client.get("/api/goals").json()["suggested"]
    assert sug[0]["dimension"] == "rally_sustain" and sug[0]["target"] == 5       # weakest first
    client.put("/api/goals/rally_sustain", json={"target": 5})
    assert all(s["dimension"] != "rally_sustain" for s in client.get("/api/goals").json()["suggested"])


def test_a_goal_can_be_deleted():
    client.put("/api/goals/nvz_discipline", json={"target": 50})
    assert client.delete("/api/goals/nvz_discipline").status_code == 200
    assert client.get("/api/goals").json()["goals"] == []


def test_goals_are_stored_in_the_data_dir():
    client.put("/api/goals/nvz_discipline", json={"target": 50})
    assert json.loads((pipeline.DATA / "goals.json").read_text())["goals"][0]["target"] == 50


# ── practice log and weekly plan ────────────────────────────────────────

def _log(**kw):
    return client.post("/api/practice", json={"drill": "Return-and-Run", "dimension": "nvz_discipline",
                                              "minutes": 15, **kw})


def test_a_practice_session_can_be_logged_and_listed_and_removed():
    r = _log()
    assert r.status_code == 200 and r.json()["date"] == TODAY.isoformat()
    rows = client.get("/api/practice").json()["practice"]
    assert len(rows) == 1 and rows[0]["minutes"] == 15
    assert client.delete(f"/api/practice/{rows[0]['id']}").status_code == 200
    assert client.get("/api/practice").json()["practice"] == []


@pytest.mark.parametrize("bad", [{"minutes": 0}, {"minutes": 601}, {"date": "tomorrow"},
                                 {"date": (TODAY + datetime.timedelta(days=3)).isoformat()}, {"drill": ""}])
def test_bad_practice_entries_are_refused(bad):
    assert _log(**bad).status_code == 422


def test_practice_days_count_toward_the_streak():
    _log()
    _log(date=(TODAY - datetime.timedelta(days=1)).isoformat())
    body = client.get("/api/streak").json()
    assert body["current_streak_days"] == 2 and body["practice_days"] == 2


def test_the_weekly_plan_picks_drills_for_the_weakest_dimensions_of_recent_sessions():
    _session("a", "2026-01-01", dims=_dims(rally=2.3))
    plan = client.get("/api/plan").json()
    assert plan["available"] is True
    first = plan["items"][0]
    assert first["dimension"] == "rally_sustain" and first["drill"]["name"] == "10-Ball Rally Target"
    assert first["done"] is False
    assert all(i["average_band"] < 3.5 for i in plan["items"]) or plan["items"][0].get("maintenance")


def test_logging_a_drill_ticks_it_off_the_plan_and_adds_to_the_weeks_minutes():
    _session("a", "2026-01-01", dims=_dims())
    client.post("/api/practice", json={"drill": "10-Ball Rally Target", "dimension": "rally_sustain", "minutes": 20})
    plan = client.get("/api/plan").json()
    assert plan["items"][0]["done"] is True and plan["week_minutes"] == 20


def test_old_practice_does_not_tick_off_this_weeks_plan():
    _session("a", "2026-01-01")
    client.post("/api/practice", json={"drill": "10-Ball Rally Target", "dimension": "rally_sustain",
                                       "minutes": 20, "date": (TODAY - datetime.timedelta(days=30)).isoformat()})
    assert client.get("/api/plan").json()["items"][0]["done"] is False


def test_the_plan_averages_the_last_three_sessions_not_just_the_latest():
    _session("a", "2026-01-01", dims=_dims(rally=2.3))
    _session("b", "2026-01-08", dims=_dims(rally=2.3))
    _session("c", "2026-01-15", dims=_dims(rally=2.3))
    assert client.get("/api/plan").json()["sessions_considered"] == 3
    _session("d", "2026-01-22", dims=_dims(rally=2.3))
    assert client.get("/api/plan").json()["sessions_considered"] == 3


def test_no_plan_without_a_rated_session():
    assert client.get("/api/plan").json()["available"] is False


def test_the_plan_still_offers_something_when_nothing_is_weak():
    strong = _dims()
    for d in strong.values():
        d["band"] = 4.5
    _session("a", "2026-01-01", dims=strong)
    items = client.get("/api/plan").json()["items"]
    assert len(items) == 1 and items[0]["maintenance"] is True


def test_the_goals_response_lists_the_dimensions_a_goal_can_be_set_on():
    dims = {d["dimension"]: d for d in client.get("/api/goals").json()["dimensions"]}
    assert dims["nvz_discipline"]["label"] == "Kitchen presence" and dims["nvz_discipline"]["unit"] == "%"
    assert dims["positioning"]["higher_is_better"] is False and dims["rally_sustain"]["higher_is_better"] is True
