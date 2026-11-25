"""Calibration that can't fool itself.

calibrate_rubric fit anchor shifts to the same sessions it then scored itself on, so
an overfit looked like a win; it mixed singles and doubles footage into one rubric;
it tried to fit known ratings above the rubric's 5.0 ceiling (pulling every shift
upward for nothing); and it read the repo's data/ directly instead of the configured
data dir.
"""

import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import calibrate_rubric as cr
import dupr
import pipeline


def _fake(root: Path, name: str, kitchen, transition, rally, known, match_type="singles"):
    d = root / name
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"filename": name, "known_dupr": known, "match_type": match_type}))
    (d / "metrics.json").write_text(json.dumps({
        "zone_pct": {"kitchen": kitchen, "transition": transition, "baseline": 100 - kitchen - transition},
        "active_seconds": 600, "warnings": [], "match_type": match_type}))
    (d / "events.json").write_text(json.dumps({"rally_count": 10, "avg_rally_hits": rally}))
    (d / "shots.json").write_text(json.dumps({"available": False}))


CONSISTENT = [("a", 20, 35, 5, 3.6), ("b", 35, 25, 7, 4.1), ("c", 50, 15, 10, 4.8), ("d", 10, 42, 4, 3.2),
              ("e", 28, 30, 6, 3.9), ("f", 42, 20, 8, 4.5)]


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "SESSIONS", tmp_path)          # restored afterwards (the old tests leaked this)
    return tmp_path


def test_the_default_sessions_dir_is_the_configured_data_dir():
    assert cr.SESSIONS == pipeline.SESSIONS


def test_leave_one_out_scores_each_session_without_seeing_it(root):
    for row in CONSISTENT:
        _fake(root, *row)
    sessions = cr.load_labeled()
    loo = cr.leave_one_out_mae(sessions)
    assert loo >= cr.mae(sessions, cr.fit(sessions)) - 1e-9      # never flatters like the in-sample score
    assert loo < cr.mae(sessions, {})                            # a real systematic offset does generalise


def test_labels_that_are_noise_do_not_pass_the_leave_one_out_check(root):
    rng = np.random.default_rng(4)
    for i, row in enumerate(CONSISTENT):
        _fake(root, row[0], row[1], row[2], row[3], float(np.round(rng.uniform(2.6, 4.9) * 4) / 4))
    sessions = cr.load_labeled()
    # in-sample the shifts can always shave something off; held-out they cannot beat doing nothing
    assert cr.leave_one_out_mae(sessions) >= cr.mae(sessions, {}) * 0.9


def test_match_types_are_calibrated_separately(root):
    for row in CONSISTENT[:3]:
        _fake(root, row[0], *row[1:])
    for row in CONSISTENT[3:]:
        _fake(root, "dbl_" + row[0], *row[1:], match_type="doubles")
    assert len(cr.load_labeled()) == 6
    assert {s["session"] for s in cr.load_labeled(match_type="singles")} == {"a", "b", "c"}
    assert len(cr.load_labeled(match_type="doubles")) == 3


def test_a_label_above_the_rubric_ceiling_is_set_aside_not_fitted(root):
    for row in CONSISTENT:
        _fake(root, *row)
    _fake(root, "pro", 70, 5, 14, 6.0)
    sessions = cr.load_labeled()
    usable, beyond = cr.split_beyond_range(sessions)
    assert [s["session"] for s in beyond] == ["pro"] and len(usable) == 6


def test_main_does_not_write_an_override_when_held_out_error_does_not_improve(root, monkeypatch, tmp_path, capsys):
    rng = np.random.default_rng(4)
    for row in CONSISTENT:
        _fake(root, row[0], row[1], row[2], row[3], float(np.round(rng.uniform(2.6, 4.9) * 4) / 4))
    out = tmp_path / "cal.json"
    monkeypatch.setattr(cr, "CALIBRATED_PATH", out)
    assert cr.main(["--match-type", "singles"]) == 0
    assert not out.exists()
    assert "held-out" in capsys.readouterr().out.lower()


def test_main_writes_an_override_that_records_what_it_was_fitted_on(root, monkeypatch, tmp_path):
    for row in CONSISTENT:
        _fake(root, *row)
    out = tmp_path / "cal.json"
    monkeypatch.setattr(cr, "CALIBRATED_PATH", out)
    assert cr.main(["--match-type", "singles"]) == 0
    data = json.loads(out.read_text())
    assert data["_meta"]["match_type"] == "singles" and data["_meta"]["sessions"] == 6
    assert data["_meta"]["loo_mae"] < data["_meta"]["baseline_mae"]


def test_a_calibration_only_applies_to_the_match_type_it_was_fitted_on(tmp_path, monkeypatch):
    f = tmp_path / "cal.json"
    shifted = [[v, min(b + 0.5, 5.0)] for v, b in dupr.RUBRIC["nvz_discipline"]["anchors"]]
    f.write_text(json.dumps({"nvz_discipline": {"anchors": shifted}, "_meta": {"match_type": "singles"}}))
    monkeypatch.setattr(dupr, "CALIBRATED_PATH", f)
    assert dupr.active_anchors("nvz_discipline", "singles") == [tuple(a) for a in shifted]
    assert dupr.active_anchors("nvz_discipline", "doubles") == dupr.RUBRIC["nvz_discipline"]["anchors"]
    assert dupr.is_calibrated("singles") is True and dupr.is_calibrated("doubles") is False
    assert dupr.active_anchors("nvz_discipline") == [tuple(a) for a in shifted]   # no type given: as before
