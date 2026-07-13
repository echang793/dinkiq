"""Session comparison endpoint tests."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient

import server
from pipeline import SESSIONS

client = TestClient(server.app)


def _mk(sid: str, label: str, kitchen: float, dupr_band: float | None,
       stage: str = "done") -> Path:
    d = SESSIONS / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps({"label": label}))
    (d / "status.json").write_text(json.dumps({"stage": stage, "state": stage}))
    (d / "metrics.json").write_text(json.dumps({
        "zone_pct": {"kitchen": kitchen, "transition": 30.0, "baseline": 100 - kitchen - 30},
        "distance_ft": 500.0, "avg_speed_ft_s": 3.0, "coverage_pct": 50.0}))
    (d / "events.json").write_text(json.dumps(
        {"rally_count": 10, "avg_rally_hits": 5.0, "play_time_pct": 20.0}))
    (d / "points.json").write_text(json.dumps(
        {"points_won": 4, "points_lost": 2, "win_pct": 66.7}))
    if dupr_band is not None:
        (d / "dupr.json").write_text(json.dumps(
            {"available": True, "band": dupr_band, "confidence": 0.7}))
    return d


def test_compare_computes_deltas():
    a = _mk("cmp_test_a", "Session A", kitchen=20.0, dupr_band=3.0)
    b = _mk("cmp_test_b", "Session B", kitchen=45.0, dupr_band=3.5)
    try:
        r = client.get("/api/compare", params={"a": "cmp_test_a", "b": "cmp_test_b"})
        assert r.status_code == 200
        body = r.json()
        assert body["a"]["label"] == "Session A"
        assert body["b"]["label"] == "Session B"
        d = body["diffs"]["kitchen_pct"]
        assert d["a"] == 20.0 and d["b"] == 45.0 and d["delta"] == 25.0
        assert body["diffs"]["dupr_band"]["delta"] == 0.5
    finally:
        import shutil
        shutil.rmtree(a, ignore_errors=True)
        shutil.rmtree(b, ignore_errors=True)


def test_compare_missing_dupr_gives_null_delta():
    a = _mk("cmp_test_nodupr_a", "A", kitchen=20.0, dupr_band=None)
    b = _mk("cmp_test_nodupr_b", "B", kitchen=25.0, dupr_band=3.0)
    try:
        r = client.get("/api/compare", params={"a": "cmp_test_nodupr_a", "b": "cmp_test_nodupr_b"})
        assert r.status_code == 200
        d = r.json()["diffs"]["dupr_band"]
        assert d["a"] is None and d["b"] == 3.0 and d["delta"] is None
    finally:
        import shutil
        shutil.rmtree(a, ignore_errors=True)
        shutil.rmtree(b, ignore_errors=True)


def test_compare_rejects_unready_session():
    a = _mk("cmp_test_unready_a", "A", kitchen=20.0, dupr_band=3.0)
    b = _mk("cmp_test_unready_b", "B", kitchen=20.0, dupr_band=3.0, stage="tracking")
    try:
        r = client.get("/api/compare", params={"a": "cmp_test_unready_a", "b": "cmp_test_unready_b"})
        assert r.status_code == 409
    finally:
        import shutil
        shutil.rmtree(a, ignore_errors=True)
        shutil.rmtree(b, ignore_errors=True)


def test_compare_rejects_missing_session():
    a = _mk("cmp_test_only", "A", kitchen=20.0, dupr_band=3.0)
    try:
        r = client.get("/api/compare", params={"a": "cmp_test_only", "b": "does_not_exist"})
        assert r.status_code == 404
    finally:
        import shutil
        shutil.rmtree(a, ignore_errors=True)


if __name__ == "__main__":
    for fn in [test_compare_computes_deltas, test_compare_missing_dupr_gives_null_delta,
               test_compare_rejects_unready_session, test_compare_rejects_missing_session]:
        fn()
        print(f"ok {fn.__name__}")
