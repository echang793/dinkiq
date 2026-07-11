"""Calibration harness tests on synthetic labeled sessions."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import calibrate_rubric as cr


def _fake_session(root: Path, name: str, kitchen: float, transition: float,
                  rally: float, known: float) -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"filename": name, "known_dupr": known}))
    (d / "metrics.json").write_text(json.dumps({
        "zone_pct": {"kitchen": kitchen, "transition": transition, "baseline": 100 - kitchen - transition},
        "active_seconds": 600, "warnings": []}))
    (d / "events.json").write_text(json.dumps({"rally_count": 10, "avg_rally_hits": rally}))
    (d / "shots.json").write_text(json.dumps({"available": False}))


def test_fit_reduces_mae(monkeypatch=None):
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # players whose true DUPR runs ~0.5 above what current anchors would say
        _fake_session(root, "a", kitchen=20, transition=35, rally=5, known=3.6)
        _fake_session(root, "b", kitchen=35, transition=25, rally=7, known=4.1)
        _fake_session(root, "c", kitchen=50, transition=15, rally=10, known=4.8)
        _fake_session(root, "d", kitchen=10, transition=42, rally=4, known=3.2)
        cr.SESSIONS = root
        sessions = cr.load_labeled()
        assert len(sessions) == 4
        base = cr.mae(sessions, {})
        shifts = cr.fit(sessions)
        fitted = cr.mae(sessions, shifts)
        assert fitted < base, (base, fitted)
        assert fitted < 0.35, fitted  # systematic offset largely absorbed


def test_refuses_small_sample():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _fake_session(root, "only", kitchen=30, transition=25, rally=6, known=3.5)
        cr.SESSIONS = root
        assert len(cr.load_labeled()) < cr.MIN_SESSIONS


if __name__ == "__main__":
    for fn in [test_fit_reduces_mae, test_refuses_small_sample]:
        fn()
        print(f"ok {fn.__name__}")
