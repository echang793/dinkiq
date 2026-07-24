"""DUPR rubric + feedback engine tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dupr import RUBRIC, estimate, interp_band, next_anchor_target
from feedback import coach_tips, drills_for_weak, fatigue_note, synergy_tip

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


def test_next_anchor_target_increasing_dimension():
    anchors = RUBRIC["nvz_discipline"]["anchors"]  # [(5,2.5),(20,3.0),(35,3.5),(50,4.25),(65,5.0)]
    assert next_anchor_target(31.0, anchors) == (35, 3.5)
    assert next_anchor_target(47.5, anchors) == (50, 4.25)
    assert next_anchor_target(65.0, anchors) is None    # already at the top anchor
    assert next_anchor_target(90.0, anchors) is None    # past it


def test_next_anchor_target_decreasing_dimension():
    anchors = RUBRIC["positioning"]["anchors"]  # [(45,2.5),(35,3.0),(25,3.5),(15,4.25),(8,5.0)]
    assert next_anchor_target(25.0, anchors) == (15, 4.25)
    assert next_anchor_target(8.0, anchors) is None


def test_strong_player_beats_weak_player():
    strong = estimate(GOOD_METRICS, GOOD_EVENTS, GOOD_SHOTS, CAL_FULL)
    weak = estimate(WEAK_METRICS, {"rally_count": 8, "avg_rally_hits": 3.5},
                    {"available": False}, CAL_FULL)
    assert strong["available"] and weak["available"]
    assert strong["band"] > weak["band"] + 0.5, (strong["band"], weak["band"])
    assert strong["band"] <= 5.0 and weak["band"] >= 2.5
    assert strong["band"] % 0.25 == 0  # quarter-band granularity


def test_dimensions_include_unit_and_next_target():
    weak = estimate(WEAK_METRICS, {"rally_count": 8, "avg_rally_hits": 3.5},
                    {"available": False}, CAL_FULL)
    kitchen = weak["dimensions"]["nvz_discipline"]
    assert kitchen["unit"] == "%"
    assert kitchen["next_target_value"] is not None
    assert kitchen["next_target_band"] is not None
    assert kitchen["next_target_band"] > kitchen["band"]


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


def test_drills_rank_all_weak_dimensions():
    weak = estimate(WEAK_METRICS, {"rally_count": 8, "avg_rally_hits": 3.5},
                    {"available": False}, CAL_FULL)
    drills = drills_for_weak(weak)
    assert drills, "weak player must get at least one drill"
    assert drills[0]["dimension"] == "nvz_discipline"  # weakest in WEAK_METRICS
    assert drills[0]["name"] == "Return-and-Run"
    assert "reps" in drills[0] and "description" in drills[0]
    # ranked worst-first, matching coach_tips' own ranking
    bands = [d["band"] for d in drills]
    assert bands == sorted(bands)
    assert len(drills) <= 3


def test_drills_empty_when_nothing_weak():
    strong = estimate(GOOD_METRICS, GOOD_EVENTS, GOOD_SHOTS, CAL_FULL)
    assert drills_for_weak(strong) == []


def test_drills_empty_when_unavailable():
    assert drills_for_weak({"available": False}) == []


def test_fatigue_note_flags_meaningful_drop():
    curve = [{"avg_speed_ft_s": 5.0}, {"avg_speed_ft_s": 5.2},
             {"avg_speed_ft_s": 3.0}, {"avg_speed_ft_s": 2.8}]
    note = fatigue_note(curve)
    assert note is not None
    assert "second half" in note


def test_fatigue_note_none_when_steady():
    curve = [{"avg_speed_ft_s": 4.0}, {"avg_speed_ft_s": 4.1},
             {"avg_speed_ft_s": 3.9}, {"avg_speed_ft_s": 4.0}]
    assert fatigue_note(curve) is None


def test_fatigue_note_none_when_too_short():
    assert fatigue_note([{"avg_speed_ft_s": 5.0}, {"avg_speed_ft_s": 1.0}]) is None


def test_synergy_tip_none_when_unavailable():
    assert synergy_tip({"available": False}) is None


def test_synergy_tip_flags_high_overlap():
    tip = synergy_tip({"available": True, "coverage_overlap_pct": 50.0, "avg_separation_ft": 10.0})
    assert tip is not None and "bunching up" in tip


def test_synergy_tip_flags_low_separation():
    tip = synergy_tip({"available": True, "coverage_overlap_pct": 5.0, "avg_separation_ft": 5.0})
    assert tip is not None and "tight" in tip


def test_synergy_tip_flags_high_separation():
    tip = synergy_tip({"available": True, "coverage_overlap_pct": 5.0, "avg_separation_ft": 18.0})
    assert tip is not None and "middle open" in tip


def test_synergy_tip_none_when_balanced():
    tip = synergy_tip({"available": True, "coverage_overlap_pct": 10.0, "avg_separation_ft": 12.0})
    assert tip is None


if __name__ == "__main__":
    for fn in [test_interp_band_monotone_and_clamped, test_next_anchor_target_increasing_dimension,
               test_next_anchor_target_decreasing_dimension, test_strong_player_beats_weak_player,
               test_dimensions_include_unit_and_next_target, test_confidence_penalties,
               test_unmeasurable_dims_omitted, test_tips_target_weakest,
               test_drills_rank_all_weak_dimensions, test_drills_empty_when_nothing_weak,
               test_drills_empty_when_unavailable, test_fatigue_note_flags_meaningful_drop,
               test_fatigue_note_none_when_steady, test_fatigue_note_none_when_too_short,
               test_synergy_tip_none_when_unavailable, test_synergy_tip_flags_high_overlap,
               test_synergy_tip_flags_low_separation, test_synergy_tip_flags_high_separation,
               test_synergy_tip_none_when_balanced]:
        fn()
        print(f"ok {fn.__name__}")
