"""Round-3 feature endpoint tests: synergy, opponent scouting, point
correction, clip notes, stat card, streak, CSV export."""

import json
import shutil
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient

import server
from pipeline import SESSIONS

client = TestClient(server.app)

CORNERS = [[300.0, 150.0], [1000.0, 150.0], [1250.0, 700.0], [50.0, 700.0]]


def _basic_session(sid: str, stage: str = "done", **meta_extra) -> Path:
    d = SESSIONS / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "status.json").write_text(json.dumps({"stage": stage, "state": stage}))
    (d / "meta.json").write_text(json.dumps({"label": sid, **meta_extra}))
    (d / "metrics.json").write_text(json.dumps(
        {"zone_pct": {"kitchen": 20.0, "transition": 30.0, "baseline": 50.0},
         "distance_ft": 300.0, "avg_speed_ft_s": 3.0, "coverage_pct": 50.0}))
    (d / "events.json").write_text(json.dumps(
        {"rally_count": 5, "avg_rally_hits": 4.0, "play_time_pct": 20.0}))
    (d / "points.json").write_text(json.dumps(
        {"outcomes": [], "points_won": 3, "points_lost": 2, "win_pct": 60.0,
         "unforced_errors": 1, "hits_by_player": {"subject": 4}}))
    (d / "dupr.json").write_text(json.dumps({"available": True, "band": 3.5, "confidence": 0.5}))
    return d


# ---------------- synergy ----------------

def test_synergy_singles_session_unavailable():
    d = _basic_session("syn_singles")
    (d / "metrics.json").write_text(json.dumps({"match_type": "singles"}))
    try:
        r = client.get(f"/api/session/{d.name}/synergy")
        assert r.status_code == 200
        assert r.json()["available"] is False
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_synergy_doubles_computes_separation_and_overlap():
    d = _basic_session("syn_doubles")
    (d / "metrics.json").write_text(json.dumps({"match_type": "doubles", "partner_track_id": 7}))
    (d / "calibration.json").write_text(json.dumps({"corners_px": CORNERS, "kitchen_px": None}))
    rows = [(f, 7, 600.0, 380.0, 660.0, 420.0, 0.9, 0.0, 0.0, 0.0, 0.0) for f in range(0, 90, 3)]
    tracks = pd.DataFrame(rows, columns=["frame", "track_id", "x1", "y1", "x2", "y2",
                                         "conf", "lwx", "lwy", "rwx", "rwy"])
    tracks.to_parquet(d / "tracks.parquet", index=False)
    subj_pos = pd.DataFrame({"t": [f / 30.0 for f in range(0, 90, 3)],
                             "x": [10.0] * 30, "y": [22.0] * 30})
    subj_pos.to_parquet(d / "positions.parquet", index=False)
    try:
        r = client.get(f"/api/session/{d.name}/synergy")
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is True
        assert body["avg_separation_ft"] == 5.4
        assert body["coverage_overlap_pct"] == 0.0
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------- opponent scouting ----------------

def test_opponent_scouting_aggregates_landing_sides():
    a = _basic_session("scout_a", opponent="Riley")
    (a / "shots.json").write_text(json.dumps(
        {"opponent_shots": {"shots_tracked": 2, "side_counts": {"left": 2}}}))
    b = _basic_session("scout_b", opponent="Riley")
    (b / "shots.json").write_text(json.dumps(
        {"opponent_shots": {"shots_tracked": 1, "side_counts": {"right": 1}}}))
    try:
        r = client.get("/api/opponents/Riley/scouting")
        assert r.status_code == 200
        body = r.json()
        assert body["shots_tracked"] == 3
        assert body["side_counts"] == {"left": 2, "right": 1}
        assert body["dominant_side"] == "left"
        assert body["dominant_side_pct"] == round(100 * 2 / 3, 1)
        assert body["matches"] == 2
    finally:
        shutil.rmtree(a, ignore_errors=True)
        shutil.rmtree(b, ignore_errors=True)


def test_opponent_scouting_no_matches():
    r = client.get("/api/opponents/NobodyEver/scouting")
    assert r.status_code == 200
    body = r.json()
    assert body["matches"] == 0
    assert body["shots_tracked"] == 0
    assert body["dominant_side"] is None


# ---------------- point correction ----------------

def test_correct_point_updates_winner_and_summary():
    d = _basic_session("pt_correct")
    (d / "points.json").write_text(json.dumps({
        "outcomes": [{"server": "subject", "winner": "opp_team", "unforced_error": True},
                     {"server": "opponent1", "winner": "my_team", "unforced_error": False}],
        "points_won": 1, "points_lost": 1, "win_pct": 50.0, "unforced_errors": 1,
        "hits_by_player": {"subject": 3, "opponent1": 2}}))
    try:
        r = client.patch(f"/api/session/{d.name}/points/0",
                         json={"winner": "my_team", "unforced_error": False})
        assert r.status_code == 200
        body = r.json()
        assert body["outcomes"][0]["winner"] == "my_team"
        assert body["outcomes"][0]["corrected"] is True
        assert body["points_won"] == 2
        assert body["points_lost"] == 0
        assert body["win_pct"] == 100.0
        assert body["hits_by_player"] == {"subject": 3, "opponent1": 2}  # preserved, not recomputed
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_correct_point_out_of_range_404():
    d = _basic_session("pt_range")
    (d / "points.json").write_text(json.dumps({"outcomes": []}))
    try:
        r = client.patch(f"/api/session/{d.name}/points/0", json={"winner": "my_team"})
        assert r.status_code == 404
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_correct_point_invalid_winner_422():
    d = _basic_session("pt_invalid")
    (d / "points.json").write_text(json.dumps(
        {"outcomes": [{"server": "subject", "winner": "unknown", "unforced_error": None}]}))
    try:
        r = client.patch(f"/api/session/{d.name}/points/0", json={"winner": "nobody"})
        assert r.status_code == 422
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------- clip notes ----------------

def test_set_and_clear_clip_note():
    d = _basic_session("clip_note")
    try:
        r = client.patch(f"/api/session/{d.name}/clip/2/note", json={"note": "watch the footwork"})
        assert r.status_code == 200
        assert r.json()["clip_notes"] == {"2": "watch the footwork"}
        meta = json.loads((d / "meta.json").read_text())
        assert meta["clip_notes"] == {"2": "watch the footwork"}

        r2 = client.patch(f"/api/session/{d.name}/clip/2/note", json={"note": ""})
        assert r2.status_code == 200
        assert r2.json()["clip_notes"] == {}
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------- stat card PNG ----------------

def test_stat_card_returns_png():
    d = _basic_session("card_test")
    try:
        r = client.get(f"/api/session/{d.name}/card.png")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_stat_card_not_ready_409():
    d = _basic_session("card_pending", stage="tracking")
    try:
        r = client.get(f"/api/session/{d.name}/card.png")
        assert r.status_code == 409
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------- streak ----------------

def test_streak_response_shape():
    r = client.get("/api/streak")
    assert r.status_code == 200
    body = r.json()
    for key in ("current_streak_days", "longest_streak_days", "sessions_this_week", "days_played"):
        assert key in body


def test_streak_counts_consecutive_days():
    import datetime
    today = datetime.date.today()
    a = _basic_session("streak_a", player="You", played_at=(today - datetime.timedelta(days=1)).isoformat())
    b = _basic_session("streak_b", player="You", played_at=today.isoformat())
    try:
        r = client.get("/api/streak")
        assert r.status_code == 200
        assert r.json()["current_streak_days"] >= 2
    finally:
        shutil.rmtree(a, ignore_errors=True)
        shutil.rmtree(b, ignore_errors=True)


# ---------------- CSV export ----------------

def test_export_csv_contains_session_row():
    d = _basic_session("csv_test", player="Casey")
    try:
        r = client.get("/api/sessions/export.csv")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        text = r.content.decode()
        assert "session_id" in text.splitlines()[0]
        assert "csv_test" in text
        assert "Casey" in text
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    for fn in [test_synergy_singles_session_unavailable, test_synergy_doubles_computes_separation_and_overlap,
               test_opponent_scouting_aggregates_landing_sides, test_opponent_scouting_no_matches,
               test_correct_point_updates_winner_and_summary, test_correct_point_out_of_range_404,
               test_correct_point_invalid_winner_422, test_set_and_clear_clip_note,
               test_stat_card_returns_png, test_stat_card_not_ready_409,
               test_streak_response_shape, test_streak_counts_consecutive_days,
               test_export_csv_contains_session_row]:
        fn()
        print(f"ok {fn.__name__}")
