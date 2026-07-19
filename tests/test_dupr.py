"""DUPR rubric + feedback engine tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dupr import RUBRIC, estimate, interp_band
from feedback import coach_tips, drill_for_weakest

GOOD_METRICS = {"zone_pct": {"kitchen": 55.0, "transition": 12.0, "baseline": 33.0},
                "active_seconds": 900.0, "warnings": []}
GOOD_EVENTS = {"rally_count": 12, "avg_rally_hits": 9.0}
GOOD_SHOTS = {"available": True, "shot_mix": {"drive": 5, "dink": 20, "drop": 6},
              "serves_measured": 8, "deep_serve_pct": 75.0}
WEAK_METRICS = {"zone_pct": {"kitchen": 8.0, "transition": 42.0, "baseline": 50.0},
                "active_seconds": 900.0, "warnings": []}
CAL_FULL = {"corners_px": [[10, 10], [1000, 10], [1200, 700], [5, 700]],
            "kitchen_px": [[300, 300], [900, 300], [1000, 450], [200, 450]]}


def test_interp_band_monotone_and_clamped():
    anchors = RUBRIC["nvz_discipline"]["anchors"]
    assert interp_band(0, anchors) == 2.5          # clamped low
    assert interp_band(99, anchors) == 5.0         # clamped high
    assert interp_band(20, anchors) == 3.0         # exact anchor
    assert interp_band(27.5, anchors) == 3.25      # midpoint interp
    # decreasing-metric dimension (lower transition% = better)
    pos = RUBRIC["positioning"]["anchors"]
    assert interp_band(45, pos) == 2.5 and interp_band(8, pos) == 5.0


def test_strong_player_beats_weak_player():
    strong = estimate(GOOD_METRICS, GOOD_EVENTS, GOOD_SHOTS, CAL_FULL)
    weak = estimate(WEAK_METRICS, {"rally_count": 8, "avg_rally_hits": 3.5},
                    {"available": False}, CAL_FULL)
    assert strong["available"] and weak["available"]
    assert strong["band"] > weak["band"] + 0.5, (strong["band"], weak["band"])
    assert strong["band"] <= 5.0 and weak["band"] >= 2.5
    assert strong["band"] % 0.25 == 0  # quarter-band granularity


def test_confidence_penalties():
    full = estimate(GOOD_METRICS, GOOD_EVENTS, GOOD_SHOTS, CAL_FULL)
    no_kitchen = estimate(GOOD_METRICS, GOOD_EVENTS, GOOD_SHOTS,
                          {"corners_px": CAL_FULL["corners_px"], "kitchen_px": None})
    offframe = estimate(GOOD_METRICS, GOOD_EVENTS, GOOD_SHOTS,
                        {"corners_px": [[-50, -20], [1000, 10], [1200, 700], [5, 700]],
                         "kitchen_px": CAL_FULL["kitchen_px"]})
    short = estimate({**GOOD_METRICS, "active_seconds": 60.0},
                     GOOD_EVENTS, GOOD_SHOTS, CAL_FULL)
    warned = estimate({**GOOD_METRICS,
                       "warnings": ["Subject projects behind the baseline most of the video"]},
                      GOOD_EVENTS, GOOD_SHOTS, CAL_FULL)
    cutty = estimate({**GOOD_METRICS, "camera_cuts": 12},
                     GOOD_EVENTS, GOOD_SHOTS, CAL_FULL)
    doubles = estimate({**GOOD_METRICS, "secondary_court_tracks": 1},
                       GOOD_EVENTS, GOOD_SHOTS, CAL_FULL)
    assert no_kitchen["confidence"] < full["confidence"]
    assert offframe["confidence"] < full["confidence"]
    assert short["confidence"] < full["confidence"]
    assert warned["confidence"] < full["confidence"]
    assert cutty["confidence"] < full["confidence"]
    assert doubles["confidence"] < full["confidence"]
    assert any("off-frame" in c for c in offframe["caveats"])
    assert any("Kitchen corners were not marked" in c for c in no_kitchen["caveats"])


def test_unmeasurable_dims_omitted():
    r = estimate(GOOD_METRICS, {"rally_count": 1}, {"available": False}, CAL_FULL)
    assert "rally_sustain" not in r["dimensions"]
    assert "shot_variety" not in r["dimensions"]
    assert r["available"]  # kitchen/positioning still enough for an estimate


def test_tips_target_weakest():
    weak = estimate(WEAK_METRICS, {"rally_count": 8, "avg_rally_hits": 3.5},
                    {"available": False}, CAL_FULL)
    weak["tips"] = tips = coach_tips(weak)
    assert tips, "weak player must get tips"
    assert any("kitchen" in t.lower() for t in tips)  # worst dimension addressed
    strong = estimate(GOOD_METRICS, GOOD_EVENTS, GOOD_SHOTS, CAL_FULL)
    stips = coach_tips(strong)
    assert stips and all("only" not in t.lower()[:30] for t in stips[:1])


def test_drill_targets_weakest_dimension():
    weak = estimate(WEAK_METRICS, {"rally_count": 8, "avg_rally_hits": 3.5},
                    {"available": False}, CAL_FULL)
    drill = drill_for_weakest(weak)
    assert drill is not None
    assert drill["dimension"] == "nvz_discipline"  # weakest in WEAK_METRICS
    assert drill["name"] == "Return-and-Run"
    assert "reps" in drill and "description" in drill


def test_drill_none_when_nothing_weak():
    strong = estimate(GOOD_METRICS, GOOD_EVENTS, GOOD_SHOTS, CAL_FULL)
    assert drill_for_weakest(strong) is None


def test_drill_none_when_unavailable():
    assert drill_for_weakest({"available": False}) is None


if __name__ == "__main__":
    for fn in [test_interp_band_monotone_and_clamped, test_strong_player_beats_weak_player,
               test_confidence_penalties, test_unmeasurable_dims_omitted,
               test_tips_target_weakest, test_drill_targets_weakest_dimension,
               test_drill_none_when_nothing_weak, test_drill_none_when_unavailable]:
        fn()
        print(f"ok {fn.__name__}")
