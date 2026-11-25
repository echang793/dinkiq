"""An estimate that says how much it knows.

The sample (a known 6.0) rated 3.5 with 45% confidence: a point value with no
range, a confidence that ignored how much live play there was, who hit the ball, or
whether the right players had been found; zero detected shots scored as "no shot
variety" (band 2.5); a single glitch "drop" earned variety credit; "next target"
could equal the current band; and a 5.0 ceiling that makes a 6.0 impossible to read
correctly was only visible in the source.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import dupr
from dupr import (BAND_MAX, RUBRIC, confidence_level, estimate, extract_dimension_values,
                  next_anchor_target, range_half_width)

METRICS = {"zone_pct": {"kitchen": 55.0, "transition": 12.0, "baseline": 33.0},
           "active_seconds": 900.0, "warnings": [], "identity_issues": []}
RALLIES = [{"start": float(10 * i), "end": float(10 * i + 8), "hits": 9, "duration": 8.0} for i in range(12)]
EVENTS = {"rally_count": 12, "avg_rally_hits": 9.0, "rallies": RALLIES}
SHOTS = {"available": True, "shot_mix": {"drive": 5, "dink": 20, "drop": 6}, "serves_measured": 8, "deep_serve_pct": 75.0}
CAL = {"corners_px": [[10, 10], [1000, 10], [1200, 700], [5, 700]],
       "kitchen_px": [[300, 300], [900, 300], [1000, 450], [200, 450]]}


def _est(metrics=None, events=None, shots=None, hits=None):
    return estimate({**METRICS, **(metrics or {})}, {**EVENTS, **(events or {})}, SHOTS if shots is None else shots,
                    CAL, hits=hits)


# ── a range, not a point ────────────────────────────────────────────────

@pytest.mark.parametrize("conf,half", [(0.95, 0.25), (0.75, 0.5), (0.45, 0.75), (0.05, 1.25)])
def test_the_range_widens_as_confidence_falls(conf, half):
    assert range_half_width(conf) == half


def test_the_estimate_carries_a_range_clamped_to_the_rubric():
    r = _est()
    assert r["band_low"] <= r["band"] <= r["band_high"]
    assert dupr.BAND_MIN <= r["band_low"] and r["band_high"] <= BAND_MAX
    assert r["band_high"] - r["band_low"] >= 0.25


def test_a_low_confidence_estimate_has_a_wider_range_than_a_confident_one():
    solid = _est()
    shaky = _est(metrics={"camera_cuts": 12, "identity_issues": ["partner_not_found"], "active_seconds": 60.0},
                 events={"rallies": RALLIES[:2], "rally_count": 2})
    assert (shaky["band_high"] - shaky["band_low"]) > (solid["band_high"] - solid["band_low"])
    assert shaky["confidence_level"] == "low" and solid["confidence_level"] in ("medium", "high")


@pytest.mark.parametrize("conf,level", [(0.1, "low"), (0.39, "low"), (0.4, "medium"), (0.69, "medium"), (0.7, "high"), (0.95, "high")])
def test_confidence_is_also_given_in_words(conf, level):
    assert confidence_level(conf) == level


def test_the_ceiling_of_the_rubric_is_stated_not_hidden():
    r = estimate({"zone_pct": {"kitchen": 80.0, "transition": 4.0, "baseline": 16.0}, "active_seconds": 900.0,
                  "warnings": []}, {**EVENTS, "avg_rally_hits": 20.0}, {"available": True, "shot_mix": {"drive": 9, "dink": 9, "drop": 9},
                                            "serves_measured": 8, "deep_serve_pct": 100.0}, CAL)
    assert r["at_ceiling"] is True and any("5.0" in c and "stronger" in c for c in r["caveats"])
    assert _est()["at_ceiling"] is False


def test_an_uncalibrated_rubric_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(dupr, "CALIBRATED_PATH", tmp_path / "none.json")
    r = _est()
    assert r["calibrated"] is False and any("calibrat" in c.lower() for c in r["caveats"])
    (tmp_path / "cal.json").write_text(json.dumps({}))
    monkeypatch.setattr(dupr, "CALIBRATED_PATH", tmp_path / "cal.json")
    assert _est()["calibrated"] is True


# ── confidence follows the evidence ─────────────────────────────────────

def test_little_live_play_lowers_confidence():
    assert _est(events={"rallies": RALLIES[:2]})["confidence"] < _est()["confidence"]       # 16 s of live play
    assert _est(events={"rallies": RALLIES[:5]})["confidence"] < _est()["confidence"]       # 40 s


def test_few_rallies_lower_confidence():
    assert _est(events={"rally_count": 3, "rallies": RALLIES[:3]})["confidence"] < _est()["confidence"]


def test_unattributed_hits_lower_confidence():
    mostly_unknown = [{"t": float(i), "hitter": "unknown" if i % 4 else "subject", "rally": 0, "n": i} for i in range(20)]
    attributed = [{"t": float(i), "hitter": "subject", "rally": 0, "n": i} for i in range(20)]
    low = _est(hits=mostly_unknown)
    assert low["confidence"] < _est(hits=attributed)["confidence"]
    assert any("attributed" in c for c in low["caveats"])


def test_an_unresolved_player_lowers_confidence():
    assert _est(metrics={"identity_issues": ["partner_not_found"]})["confidence"] < _est()["confidence"]


def test_missing_evidence_fields_are_not_penalised_as_if_they_were_bad():
    """Older sessions have no hits.json / rallies list: absence isn't evidence of weakness."""
    plain = estimate({"zone_pct": METRICS["zone_pct"], "active_seconds": 900.0, "warnings": []},
                     {"rally_count": 12, "avg_rally_hits": 9.0}, SHOTS, CAL)
    assert plain["confidence"] == _est()["confidence"]


# ── too little to rate ──────────────────────────────────────────────────

def test_a_clip_of_under_45_seconds_is_not_rated():
    r = _est(metrics={"active_seconds": 30.0})
    assert r["available"] is False and "45" in r["reason"]


def test_a_clip_just_over_the_minimum_is_rated_with_low_confidence():
    r = _est(metrics={"active_seconds": 50.0})
    assert r["available"] is True and r["confidence"] < _est()["confidence"]


# ── dimensions only count when they were measured ───────────────────────

def test_one_glitch_shot_does_not_earn_variety_credit():
    vals = extract_dimension_values(METRICS, EVENTS, {"available": True, "shot_mix": {"drop": 1}})
    assert "shot_variety" not in vals


def test_a_type_needs_three_shots_to_count_and_there_must_be_enough_shots():
    few = extract_dimension_values(METRICS, EVENTS, {"available": True, "shot_mix": {"drive": 2, "dink": 2, "drop": 1}})
    assert "shot_variety" not in few
    enough = extract_dimension_values(METRICS, EVENTS, {"available": True, "shot_mix": {"drive": 8, "dink": 7, "drop": 2}})
    assert enough["shot_variety"] == 2.0               # drive and dink earned it; two drops did not


def test_zero_detected_shots_is_not_scored_as_no_variety():
    r = _est(shots={"available": True, "shot_mix": {}})
    assert "shot_variety" not in r["dimensions"]


def test_a_session_without_audio_rates_on_positioning_alone_and_says_less():
    r = estimate(METRICS, {"rally_count": 0}, {"available": False}, CAL)
    assert r["available"] and set(r["dimensions"]) == {"nvz_discipline", "positioning"}
    assert r["confidence"] < _est()["confidence"]


# ── next target is a genuinely better tier ──────────────────────────────

def test_the_next_target_is_never_the_same_band_as_now():
    anchors = RUBRIC["rally_sustain"]["anchors"]       # [(3, 2.5), (5, 3.0), ...]
    # 2.2 hits is below the first anchor: it already reads as the 2.5 floor, so (3 hits -> 2.5) is no "next tier"
    assert next_anchor_target(2.2, anchors, current_band=2.5) == (5, 3.0)
    assert next_anchor_target(6.0, anchors, current_band=3.25) == (7, 3.5)


def test_the_estimate_never_reports_a_next_target_at_or_below_the_current_band():
    r = estimate({**METRICS, "zone_pct": {"kitchen": 2.0, "transition": 50.0, "baseline": 48.0}},
                 {"rally_count": 8, "avg_rally_hits": 2.2, "rallies": RALLIES}, {"available": False}, CAL)
    for name, d in r["dimensions"].items():
        if d["next_target_band"] is not None:
            assert d["next_target_band"] > d["band"], (name, d)


# ── wording ─────────────────────────────────────────────────────────────

def test_notifications_say_resembles_not_plays_like(tmp_path):
    import notify
    (tmp_path / "dupr.json").write_text(json.dumps({"band": 3.5, "confidence": 0.45, "available": True}))
    (tmp_path / "meta.json").write_text("{}")
    msg = notify.notify_message(tmp_path, ok=True)
    assert "resembles" in msg.lower() and "plays like" not in msg.lower() and "45%" not in msg
    assert "medium" in msg.lower() or "low" in msg.lower()


def test_the_stat_card_says_resembles_not_estimated_dupr():
    import card
    png = card.render_stat_card({"label": "x"}, {"zone_pct": {"kitchen": 40.0}}, {"rally_count": 3}, {},
                                {"available": True, "band": 3.5, "confidence": 0.3, "band_low": 2.75, "band_high": 4.25})
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    src = Path(card.__file__).read_text()
    assert "estimated DUPR" not in src and "resembles" in src
