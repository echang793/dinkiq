"""PDF report generation tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient

import server
from pipeline import SESSIONS
from report import build_report_pdf

META = {"label": "Test Session", "played_at": "2026-07-11", "filename": "t.mp4"}
METRICS = {"zone_pct": {"kitchen": 42.0, "transition": 20.0, "baseline": 38.0},
           "distance_ft": 1234.5, "coverage_pct": 61.0, "warnings": []}
EVENTS = {"rally_count": 12, "avg_rally_hits": 5.3, "play_time_pct": 22.0}
SHOTS = {"available": False}
POINTS = {"points_scored": 8, "points_won": 5, "points_lost": 3, "win_pct": 62.5,
          "unforced_errors": 2}
DUPR = {"available": True, "band": 3.75, "confidence": 0.72,
        "dimensions": {"a": {"label": "Kitchen presence", "band": 4.0},
                       "b": {"label": "Rally consistency", "band": 3.2}},
        "caveats": ["Short sample"],
        "tips": ["Work on your third shot drop.", "Get to the kitchen faster."]}


def test_build_report_pdf_valid_bytes():
    pdf = build_report_pdf(META, METRICS, EVENTS, SHOTS, POINTS, DUPR)
    assert pdf[:5] == b"%PDF-"
    assert pdf.rstrip().endswith(b"%%EOF")
    assert len(pdf) > 500


def test_build_report_pdf_handles_missing_dupr():
    pdf = build_report_pdf(META, METRICS, EVENTS, SHOTS, POINTS, {})
    assert pdf[:5] == b"%PDF-"


def test_build_report_pdf_handles_empty_everything():
    pdf = build_report_pdf({}, {}, {}, {}, {}, {})
    assert pdf[:5] == b"%PDF-"


def test_report_endpoint_serves_pdf():
    sid = "test_report_session"
    sdir = SESSIONS / sid
    sdir.mkdir(parents=True, exist_ok=True)
    try:
        (sdir / "meta.json").write_text('{"label": "Endpoint Test"}')
        (sdir / "status.json").write_text('{"stage": "done", "state": "done"}')
        (sdir / "metrics.json").write_text('{"zone_pct": {"kitchen": 10.0}}')
        (sdir / "events.json").write_text('{"rally_count": 0}')
        (sdir / "shots.json").write_text('{"available": false}')
        (sdir / "points.json").write_text('{"points_scored": 0}')
        (sdir / "dupr.json").write_text('{"available": false, "reason": "n/a"}')

        client = TestClient(server.app)
        r = client.get(f"/api/session/{sid}/report.pdf")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/pdf"
        assert r.content[:5] == b"%PDF-"
    finally:
        import shutil
        shutil.rmtree(sdir, ignore_errors=True)


def test_report_endpoint_404_before_done():
    sid = "test_report_notdone"
    sdir = SESSIONS / sid
    sdir.mkdir(parents=True, exist_ok=True)
    try:
        (sdir / "status.json").write_text('{"stage": "tracking", "state": "running"}')
        client = TestClient(server.app)
        r = client.get(f"/api/session/{sid}/report.pdf")
        assert r.status_code == 409
    finally:
        import shutil
        shutil.rmtree(sdir, ignore_errors=True)


if __name__ == "__main__":
    for fn in [test_build_report_pdf_valid_bytes, test_build_report_pdf_handles_missing_dupr,
               test_build_report_pdf_handles_empty_everything, test_report_endpoint_serves_pdf,
               test_report_endpoint_404_before_done]:
        fn()
        print(f"ok {fn.__name__}")
