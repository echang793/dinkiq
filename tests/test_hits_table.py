"""hits.json: the per-hit table (time, who hit it, which rally, position in it).

events() computed every hit's time and attributed hitter, used them for rallies and
points, then discarded them -- so a corrected point couldn't be recomputed, and
"show me the clips behind this stat" had nothing to join on.
"""

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pipeline
from events import hits_table

RALLIES = [{"start": 1.0, "end": 3.0, "hits": 3, "duration": 2.0},
           {"start": 10.0, "end": 11.0, "hits": 2, "duration": 1.0}]


def test_each_hit_gets_its_rally_and_position_within_it():
    t = np.array([1.0, 2.0, 3.0, 6.5, 10.0, 11.0])
    who = ["subject", "opponent1", "subject", "unknown", "opponent1", "subject"]
    rows = hits_table(t, who, RALLIES)
    assert [(r["rally"], r["n"]) for r in rows] == [(0, 0), (0, 1), (0, 2), (None, None), (1, 0), (1, 1)]
    assert [r["hitter"] for r in rows] == who
    assert [r["t"] for r in rows] == [1.0, 2.0, 3.0, 6.5, 10.0, 11.0]


def test_a_hit_outside_every_rally_is_kept_with_no_rally():
    """Isolated hits (a serve fault, a lone pop) aren't dropped: they're the unreturned serves."""
    rows = hits_table(np.array([50.0]), ["unknown"], RALLIES)
    assert rows == [{"t": 50.0, "hitter": "unknown", "rally": None, "n": None}]


def test_empty_inputs_give_an_empty_table():
    assert hits_table(np.array([]), [], []) == []


def test_times_are_plain_floats_so_they_serialise():
    rows = hits_table(np.array([1.234567]), ["subject"], RALLIES)
    assert type(rows[0]["t"]) is float
    json.dumps(rows, allow_nan=False)


def test_hits_json_is_cleared_with_the_other_derived_results(tmp_path):
    (tmp_path / "hits.json").write_text("[]")
    (tmp_path / "metrics.json").write_text("{}")
    pipeline.clear_derived(tmp_path)
    assert not (tmp_path / "hits.json").exists()
