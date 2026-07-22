"""Cross-session trend endpoint: /api/progress, incl. weakest-dimension surfacing."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient

import server
from pipeline import SESSIONS

client = TestClient(server.app)


def _mk_done(sid: str, played_at: str, kitchen: float, drills: list[dict] | None,
            dimensions: dict | None = None) -> Path:
    d = SESSIONS / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps({"label": sid, "played_at": played_at}))
    (d / "status.json").write_text(json.dumps({"stage": "done", "state": "done"}))
    (d / "metrics.json").write_text(json.dumps(
        {"zone_pct": {"kitchen": kitchen, "transition": 20.0, "baseline": 100 - kitchen - 20},
         "distance_ft": 400.0}))
    (d / "events.json").write_text(json.dumps({"avg_rally_hits": 5.0}))
    dupr = {"available": True, "band": 3.5, "confidence": 0.6}
    if drills is not None:
        dupr["drills"] = drills
    if dimensions is not None:
        dupr["dimensions"] = dimensions
    (d / "dupr.json").write_text(json.dumps(dupr))
    return d


def test_progress_surfaces_weakest_dimension():
    a = _mk_done("prog_a", "2026-07-01", 20.0,
                [{"dimension": "nvz_discipline", "target_label": "Kitchen presence"}])
    b = _mk_done("prog_b", "2026-07-08", 25.0,
                [{"dimension": "nvz_discipline", "target_label": "Kitchen presence"}])
    c = _mk_done("prog_c", "2026-07-15", 30.0, None)  # no drills computed (strong session)
    try:
        r = client.get("/api/progress")
        assert r.status_code == 200
        pts = {p["session_id"]: p for p in r.json()}
        assert pts["prog_a"]["weakest_dimension"] == "nvz_discipline"
        assert pts["prog_a"]["weakest_label"] == "Kitchen presence"
        assert pts["prog_b"]["weakest_dimension"] == "nvz_discipline"
        assert pts["prog_c"]["weakest_dimension"] is None
        # chronological order
        ids = [p["session_id"] for p in r.json() if p["session_id"] in ("prog_a", "prog_b", "prog_c")]
        assert ids == ["prog_a", "prog_b", "prog_c"]
    finally:
        import shutil
        for d in (a, b, c):
            shutil.rmtree(d, ignore_errors=True)


def test_progress_surfaces_per_dimension_bands():
    a = _mk_done("prog_dims", "2026-07-01", 20.0, None,
                dimensions={"nvz_discipline": {"label": "Kitchen presence", "band": 3.0}})
    try:
        r = client.get("/api/progress")
        assert r.status_code == 200
        pt = next(p for p in r.json() if p["session_id"] == "prog_dims")
        assert pt["dimensions"]["nvz_discipline"]["band"] == 3.0
    finally:
        import shutil
        shutil.rmtree(a, ignore_errors=True)


def test_progress_empty_when_no_sessions():
    r = client.get("/api/progress")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


if __name__ == "__main__":
    for fn in [test_progress_surfaces_weakest_dimension, test_progress_surfaces_per_dimension_bands,
               test_progress_empty_when_no_sessions]:
        fn()
        print(f"ok {fn.__name__}")
