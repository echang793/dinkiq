"""Point corrections are the user's word against a heuristic: they must outlive a reprocess.

They used to be written straight into points.json, which is derived and deleted by
reprocess -- so re-analyzing a session silently threw away every manual fix.
"""

import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient

import corrections
import pipeline
import server
import stages
from pipeline import SESSIONS

client = TestClient(server.app)

RALLIES = [{"start": 10.0, "end": 14.0, "hits": 4, "duration": 4.0},
           {"start": 30.0, "end": 33.0, "hits": 3, "duration": 3.0}]


def _session(sid="cor") -> Path:
    d = SESSIONS / sid
    d.mkdir(parents=True)
    (d / "status.json").write_text(json.dumps({"stage": "done", "state": "done"}))
    (d / "meta.json").write_text(json.dumps({"label": sid}))
    (d / "events.json").write_text(json.dumps({"rallies": RALLIES}))
    (d / "shots.json").write_text(json.dumps({"available": False}))
    (d / "points.json").write_text(json.dumps({
        "outcomes": [
            {"server": "subject", "last_hitter": "subject", "winner": "opp_team", "unforced_error": True},
            {"server": "opponent1", "last_hitter": "opponent1", "winner": "unknown", "unforced_error": None}],
        "points_scored": 1, "points_won": 0, "points_lost": 1, "win_pct": 0.0,
        "unforced_errors": 1, "hits_by_player": {"subject": 4}}))
    return d


def _patch(sid, idx, **body):
    return client.patch(f"/api/session/{sid}/points/{idx}", json=body)


def test_a_correction_is_stored_apart_from_the_derived_points_file():
    d = _session()
    assert _patch(d.name, 0, winner="my_team", unforced_error=False).status_code == 200
    stored = corrections.load(d)
    assert len(stored) == 1
    assert stored[0]["start"] == 10.0 and stored[0]["winner"] == "my_team"


def test_reprocess_clears_points_json_but_not_the_corrections():
    d = _session()
    _patch(d.name, 0, winner="my_team")
    pipeline.clear_derived(d)
    assert not (d / "points.json").exists()
    assert (d / corrections.FILE).exists()


def test_the_points_stage_reapplies_corrections_to_freshly_detected_rallies():
    d = _session()
    _patch(d.name, 1, winner="my_team", unforced_error=False)
    (d / "points.json").unlink()                      # what a reprocess does
    stages.points_stage(d, RALLIES, np.array([10.0, 14.0, 30.0, 33.0]),
                        ["subject", "opponent1", "opponent1", "opponent1"],
                        bounces_court=None, ball_available=False, shots_report={}, pos=None)
    out = json.loads((d / "points.json").read_text())
    assert out["outcomes"][1]["winner"] == "my_team" and out["outcomes"][1]["corrected"] is True
    assert out["outcomes"][0]["winner"] == "unknown"  # nothing was said about rally 0
    assert out["points_won"] == 1                     # and the summary counts the fix


def test_a_correction_still_matches_when_the_rally_boundaries_shift_slightly():
    d = _session()
    _patch(d.name, 0, winner="my_team")
    shifted = [{"start": 10.4, "end": 14.2, "hits": 4, "duration": 3.8}] + RALLIES[1:]
    outcomes = [{"server": "subject", "last_hitter": "subject", "winner": "unknown", "unforced_error": None},
                {"server": "subject", "last_hitter": "subject", "winner": "unknown", "unforced_error": None}]
    fixed = corrections.apply(outcomes, shifted, corrections.load(d))
    assert fixed[0]["winner"] == "my_team" and fixed[1]["winner"] == "unknown"


def test_a_correction_for_a_rally_that_no_longer_exists_is_ignored_not_fatal():
    d = _session()
    _patch(d.name, 0, winner="my_team")
    outcomes = [{"server": "subject", "last_hitter": "subject", "winner": "unknown", "unforced_error": None}]
    far_away = [{"start": 500.0, "end": 503.0, "hits": 3, "duration": 3.0}]
    assert corrections.apply(outcomes, far_away, corrections.load(d))[0]["winner"] == "unknown"


def test_one_correction_never_lands_on_two_rallies():
    c = [{"start": 10.0, "winner": "my_team", "unforced_error": None}]
    outcomes = [{"winner": "unknown", "unforced_error": None}] * 2
    close = [{"start": 10.0, "end": 12.0}, {"start": 10.5, "end": 12.5}]
    fixed = corrections.apply(outcomes, close, c)
    assert [o["winner"] for o in fixed] == ["my_team", "unknown"]


def test_a_correction_can_be_undone_back_to_what_the_heuristic_said():
    d = _session()
    _patch(d.name, 0, winner="my_team", unforced_error=False)
    r = client.delete(f"/api/session/{d.name}/points/0")
    assert r.status_code == 200
    body = r.json()
    assert body["outcomes"][0]["winner"] == "opp_team" and body["outcomes"][0]["unforced_error"] is True
    assert "corrected" not in body["outcomes"][0]
    assert body["points_lost"] == 1
    assert corrections.load(d) == []


def test_undoing_a_rally_that_was_never_corrected_is_a_harmless_no_op():
    d = _session()
    r = client.delete(f"/api/session/{d.name}/points/0")
    assert r.status_code == 200 and r.json()["outcomes"][0]["winner"] == "opp_team"


def test_correcting_twice_keeps_the_original_heuristic_answer_for_undo():
    d = _session()
    _patch(d.name, 0, winner="my_team")
    _patch(d.name, 0, winner="unknown")
    assert len(corrections.load(d)) == 1
    body = client.delete(f"/api/session/{d.name}/points/0").json()
    assert body["outcomes"][0]["winner"] == "opp_team"      # not "my_team", the first fix


def test_a_point_can_be_marked_unclear_and_flagged_as_an_unforced_error():
    d = _session()
    body = _patch(d.name, 0, winner="unknown", unforced_error=True).json()
    assert body["outcomes"][0]["winner"] == "unknown" and body["outcomes"][0]["unforced_error"] is True
    assert body["points_scored"] == 0


def test_undo_out_of_range_is_404():
    d = _session()
    assert client.delete(f"/api/session/{d.name}/points/9").status_code == 404


@pytest.mark.parametrize("bad", [{"winner": "my_team", "unforced_error": "maybe"}])
def test_a_junk_unforced_error_value_is_rejected(bad):
    d = _session()
    assert _patch(d.name, 0, **bad).status_code == 422
