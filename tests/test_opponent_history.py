"""Head-to-head opponent history endpoint tests."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient

import server
from pipeline import SESSIONS

client = TestClient(server.app)


def _mk(sid: str, opponent: str, won: int, lost: int, stage: str = "done") -> Path:
    d = SESSIONS / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps({"label": sid, "opponent": opponent}))
    (d / "status.json").write_text(json.dumps({"stage": stage, "state": stage}))
    (d / "metrics.json").write_text(json.dumps(
        {"zone_pct": {"kitchen": 20.0, "transition": 30.0, "baseline": 50.0},
         "distance_ft": 500.0, "avg_speed_ft_s": 3.0, "coverage_pct": 50.0}))
    (d / "events.json").write_text(json.dumps(
        {"rally_count": 10, "avg_rally_hits": 5.0, "play_time_pct": 20.0}))
    win_pct = round(100.0 * won / (won + lost), 1) if (won + lost) else None
    (d / "points.json").write_text(json.dumps(
        {"points_won": won, "points_lost": lost, "win_pct": win_pct}))
    return d


def test_opponent_history_aggregates_wins_and_losses():
    a = _mk("oh_a", "Sam", won=8, lost=4)
    b = _mk("oh_b", "Sam", won=3, lost=7)
    c = _mk("oh_c", "Alex", won=9, lost=1)  # different opponent, excluded
    try:
        r = client.get("/api/opponents/Sam/history")
        assert r.status_code == 200
        body = r.json()
        assert body["matches"] == 2
        assert body["wins"] == 1 and body["losses"] == 1
        assert body["win_pct"] == 50.0
        assert {s["session_id"] for s in body["sessions"]} == {"oh_a", "oh_b"}
    finally:
        import shutil
        for d in (a, b, c):
            shutil.rmtree(d, ignore_errors=True)


def test_opponent_history_case_insensitive():
    a = _mk("oh_case", "Jamie", won=5, lost=1)
    try:
        r = client.get("/api/opponents/jamie/history")
        assert r.status_code == 200
        assert r.json()["matches"] == 1
    finally:
        import shutil
        shutil.rmtree(a, ignore_errors=True)


def test_opponent_history_excludes_unfinished_sessions():
    a = _mk("oh_running", "Robin", won=0, lost=0, stage="tracking")
    try:
        r = client.get("/api/opponents/Robin/history")
        assert r.status_code == 200
        assert r.json()["matches"] == 0
    finally:
        import shutil
        shutil.rmtree(a, ignore_errors=True)


def test_opponent_history_no_matches():
    r = client.get("/api/opponents/NoOneEver/history")
    assert r.status_code == 200
    body = r.json()
    assert body["matches"] == 0
    assert body["win_pct"] is None
    assert body["sessions"] == []


if __name__ == "__main__":
    for fn in [test_opponent_history_aggregates_wins_and_losses,
               test_opponent_history_case_insensitive,
               test_opponent_history_excludes_unfinished_sessions,
               test_opponent_history_no_matches]:
        fn()
        print(f"ok {fn.__name__}")
