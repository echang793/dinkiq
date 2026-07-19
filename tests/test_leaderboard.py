"""Friend leaderboard endpoint tests: /api/leaderboard."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient

import server
from pipeline import SESSIONS

client = TestClient(server.app)


def _mk(sid: str, player: str | None, band: float | None, won: int, lost: int,
        stage: str = "done") -> Path:
    d = SESSIONS / sid
    d.mkdir(parents=True, exist_ok=True)
    meta = {"label": sid}
    if player is not None:
        meta["player"] = player
    (d / "meta.json").write_text(json.dumps(meta))
    (d / "status.json").write_text(json.dumps({"stage": stage, "state": stage}))
    (d / "metrics.json").write_text(json.dumps(
        {"zone_pct": {"kitchen": 20.0, "transition": 30.0, "baseline": 50.0},
         "distance_ft": 300.0, "avg_speed_ft_s": 3.0, "coverage_pct": 50.0}))
    (d / "events.json").write_text(json.dumps(
        {"rally_count": 10, "avg_rally_hits": 5.0, "play_time_pct": 20.0}))
    win_pct = round(100.0 * won / (won + lost), 1) if (won + lost) else None
    (d / "points.json").write_text(json.dumps(
        {"points_won": won, "points_lost": lost, "win_pct": win_pct}))
    dupr = {"available": band is not None}
    if band is not None:
        dupr["band"] = band
    (d / "dupr.json").write_text(json.dumps(dupr))
    return d


def test_leaderboard_aggregates_by_player_and_ranks_by_latest_band():
    a = _mk("lb_a", "Sam", 3.5, won=8, lost=4)
    b = _mk("lb_b", "Sam", 4.0, won=3, lost=7)  # later mtime -> Sam's latest band
    c = _mk("lb_c", "Alex", 4.5, won=9, lost=1)
    try:
        r = client.get("/api/leaderboard")
        assert r.status_code == 200
        rows = {row["player"]: row for row in r.json()["players"]}
        assert rows["Sam"]["sessions"] == 2
        assert rows["Sam"]["latest_band"] == 4.0
        assert rows["Sam"]["best_band"] == 4.0
        assert rows["Sam"]["win_pct"] == 50.0
        assert rows["Alex"]["sessions"] == 1
        assert rows["Alex"]["latest_band"] == 4.5
        # ranked by latest band descending
        names = [row["player"] for row in r.json()["players"]]
        assert names.index("Alex") < names.index("Sam")
    finally:
        import shutil
        for d in (a, b, c):
            shutil.rmtree(d, ignore_errors=True)


def test_leaderboard_defaults_unset_player_and_excludes_incomplete():
    a = _mk("lb_default", None, 3.0, won=5, lost=5)
    b = _mk("lb_incomplete", "Jamie", None, won=0, lost=0, stage="ingest")
    try:
        r = client.get("/api/leaderboard")
        assert r.status_code == 200
        rows = {row["player"]: row for row in r.json()["players"]}
        assert "You" in rows
        assert "Jamie" not in rows
    finally:
        import shutil
        for d in (a, b):
            shutil.rmtree(d, ignore_errors=True)


def test_leaderboard_response_shape():
    r = client.get("/api/leaderboard")
    assert r.status_code == 200
    assert isinstance(r.json().get("players"), list)


if __name__ == "__main__":
    for fn in [test_leaderboard_aggregates_by_player_and_ranks_by_latest_band,
               test_leaderboard_defaults_unset_player_and_excludes_incomplete,
               test_leaderboard_empty_when_no_sessions]:
        fn()
        print(f"ok {fn.__name__}")
