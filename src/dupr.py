"""M4 DUPR-band rubric: metrics -> "your play resembles ~X.X" estimate.

Honest framing: this is a heuristic scorecard, not a certified rating. Each
dimension maps an observable metric to a band via piecewise-linear anchors;
the overall band is the weighted mean of available dimensions. Confidence
reflects how much of the evidence chain was actually measurable.

RUBRIC is the single source of truth — tune anchors/weights here only
(calibration against known-DUPR footage adjusts these numbers, not code).
"""

from pathlib import Path

import numpy as np

from jsonio import load_json

BAND_MIN, BAND_MAX = 2.5, 5.0
# What the rubric can distinguish: 2.5-5.0. A stronger player reads 5.0 too; that is
# reported (at_ceiling) rather than extended with invented anchors above it.

MIN_ACTIVE_SECONDS = 45     # tracked time below which no level is claimed
MIN_TYPED_SHOTS = 6         # classified shots needed before "variety" means anything
MIN_SHOTS_PER_TYPE = 3      # a type counts only once it has shown up this many times

# Each dimension: weight + (metric_value, band) anchor points, monotone in value.
RUBRIC: dict[str, dict] = {
    "nvz_discipline": {          # % of time at the kitchen line
        "weight": 0.30,
        "anchors": [(5, 2.5), (20, 3.0), (35, 3.5), (50, 4.25), (65, 5.0)],
        "label": "Kitchen presence",
        "unit": "%",
    },
    "positioning": {             # % of time in no-man's land (lower = better)
        "weight": 0.20,
        "anchors": [(45, 2.5), (35, 3.0), (25, 3.5), (15, 4.25), (8, 5.0)],
        "label": "Court positioning",
        "unit": "%",
    },
    "rally_sustain": {           # average hits per rally
        "weight": 0.20,
        "anchors": [(3, 2.5), (5, 3.0), (7, 3.5), (10, 4.25), (14, 5.0)],
        "label": "Rally consistency",
        "unit": " hits",
    },
    "shot_variety": {            # distinct shot types used (of drive/dink/drop)
        "weight": 0.15,
        "anchors": [(0, 2.5), (1, 3.0), (2, 3.75), (3, 4.5)],
        "label": "Shot variety",
        "unit": " types",
    },
    "serve_depth": {             # % of measured serves landing within 8ft of baseline
        "weight": 0.15,
        "anchors": [(0, 2.75), (25, 3.0), (50, 3.5), (75, 4.25), (95, 5.0)],
        "label": "Serve depth",
        "unit": "%",
    },
}


CALIBRATED_PATH = Path(__file__).resolve().parent.parent / "models" / "rubric_calibrated.json"


def _calibration(match_type: str | None = None) -> dict:
    """The calibration override, or {} when absent or fitted for a different match type
    (singles and doubles footage were being mixed into one rubric)."""
    if not CALIBRATED_PATH.exists():
        return {}
    data = load_json(CALIBRATED_PATH)
    fitted_for = (data.get("_meta") or {}).get("match_type")
    if match_type and fitted_for and fitted_for != match_type:
        return {}
    return data


def is_calibrated(match_type: str | None = None) -> bool:
    return bool(_calibration(match_type))


def active_anchors(name: str, match_type: str | None = None) -> list[tuple[float, float]]:
    """Anchors for a dimension: calibrated override if present (and fitted for this
    match type), else RUBRIC.

    calibrate_rubric.py writes the override after fitting against sessions
    tagged with a known DUPR. RUBRIC stays the structural source of truth.
    """
    data = _calibration(match_type)
    if name in data:
        return [tuple(a) for a in data[name]["anchors"]]
    return RUBRIC[name]["anchors"]


def interp_band(value: float, anchors: list[tuple[float, float]]) -> float:
    """Piecewise-linear metric->band, clamped to the anchor range.

    Anchors may run in decreasing metric order (lower-is-better dimensions).
    """
    xs = np.array([a[0] for a in anchors], dtype=float)
    ys = np.array([a[1] for a in anchors], dtype=float)
    if xs[0] > xs[-1]:  # np.interp needs ascending x
        xs, ys = xs[::-1], ys[::-1]
    return float(np.clip(np.interp(value, xs, ys), BAND_MIN, BAND_MAX))


def next_anchor_target(value: float, anchors: list[tuple[float, float]],
                       current_band: float | None = None) -> tuple[float, float] | None:
    """The nearest better anchor above `value`'s current position — a
    concrete "reach X to hit the next tier" number, not just a score.
    None once `value` is at or past the top anchor (nothing further this
    rubric can distinguish). Anchors may run in decreasing metric order
    (lower-is-better dimensions like positioning), same convention as
    interp_band.

    With `current_band`, only anchors that are a genuinely higher BAND count: a value
    below the first anchor already reads as the floor band, and the "next" anchor can
    carry that same band (2.2 hits -> "reach 3 hits for 2.5", which you already are)."""
    xs = [a[0] for a in anchors]
    decreasing = xs[0] > xs[-1]
    if not decreasing:
        better = [(x, y) for x, y in anchors if x > value + 1e-9]
    else:
        better = [(x, y) for x, y in anchors if x < value - 1e-9]
    if current_band is not None:
        better = [(x, y) for x, y in better if y > current_band + 1e-9]
    if not better:
        return None
    return min(better, key=lambda p: p[0]) if not decreasing else max(better, key=lambda p: p[0])


def range_half_width(confidence: float) -> float:
    """Half the width of the shown range, in quarter bands: +-0.25 when confident, up to
    +-1.25 when almost nothing is known. A point value hid how little was behind it."""
    return round((0.25 + (1.0 - confidence)) / 0.25) * 0.25


def confidence_level(confidence: float) -> str:
    return "low" if confidence < 0.4 else "medium" if confidence < 0.7 else "high"


def extract_dimension_values(metrics: dict, events: dict, shots: dict) -> dict[str, float]:
    """Pull each rubric dimension's raw value; omit unmeasurable dimensions."""
    vals: dict[str, float] = {}
    zp = metrics.get("zone_pct", {})
    if zp:
        vals["nvz_discipline"] = float(zp.get("kitchen", 0.0))
        vals["positioning"] = float(zp.get("transition", 0.0))
    if events.get("rally_count", 0) >= 3:  # too few rallies = meaningless average
        vals["rally_sustain"] = float(events["avg_rally_hits"])
    if shots.get("available"):
        mix = shots.get("shot_mix", {})
        # zero detected shots used to score as "no variety" (band 2.5), and one glitch
        # "drop" as a whole shot type: it must be measured, in enough volume
        if sum(mix.values()) >= MIN_TYPED_SHOTS:
            vals["shot_variety"] = float(sum(1 for k in ("drive", "dink", "drop")
                                             if mix.get(k, 0) >= MIN_SHOTS_PER_TYPE))
        if shots.get("serves_measured", 0) >= 3:
            vals["serve_depth"] = float(shots["deep_serve_pct"])
    return vals


def estimate(metrics: dict, events: dict, shots: dict,
             calibration: dict | None = None, hits: list[dict] | None = None) -> dict:
    caveats: list[str] = list(metrics.get("warnings", []))
    if metrics.get("active_seconds", 0) < MIN_ACTIVE_SECONDS:
        return {"available": False,
                "reason": f"under {MIN_ACTIVE_SECONDS} s of the player was tracked — too little "
                          "to say anything about level",
                "caveats": caveats}
    vals = extract_dimension_values(metrics, events, shots)

    dims = {}
    for name, spec in RUBRIC.items():
        if name in vals:
            anchors = active_anchors(name, metrics.get("match_type"))
            band_now = interp_band(vals[name], anchors)
            target = next_anchor_target(vals[name], anchors, current_band=band_now)
            dims[name] = {"label": spec["label"], "value": round(vals[name], 1),
                          "band": round(band_now, 2),
                          "weight": spec["weight"], "unit": spec["unit"],
                          "next_target_value": round(target[0], 1) if target else None,
                          "next_target_band": round(target[1], 2) if target else None}

    if not dims or "nvz_discipline" not in dims:
        return {"available": False,
                "reason": "not enough measurable play to estimate a level",
                "caveats": caveats}

    wsum = sum(d["weight"] for d in dims.values())
    band = sum(d["band"] * d["weight"] for d in dims.values()) / wsum

    # confidence: starts from dimension coverage, then docked for each way the evidence is thin.
    # A missing evidence field (older sessions) is not penalised as if it were bad.
    confidence = 0.30 + 0.65 * (wsum / sum(s["weight"] for s in RUBRIC.values()))
    if metrics.get("active_seconds", 0) < 300:
        caveats.append("Short sample (<5 min tracked) — estimate is volatile.")
        confidence -= 0.10
    rallies = events.get("rallies")
    if rallies is not None:
        live_s = sum(r.get("duration", 0.0) for r in rallies)
        if live_s < 30:
            caveats.append(f"Only {live_s:.0f} s of live play was detected — the level is inferred "
                           "from very little actual rallying.")
            confidence -= 0.15
        elif live_s < 60:
            confidence -= 0.08
    if 0 < events.get("rally_count", 0) < 5 or (rallies is not None and not rallies):
        confidence -= 0.08
    if hits and len(hits) >= 6:
        unknown = sum(1 for h in hits if h.get("hitter") == "unknown") / len(hits)
        if unknown > 0.5:
            caveats.append("Most paddle hits couldn't be attributed to a player, so shot and "
                           "rally figures are unreliable.")
            confidence -= 0.10
    if metrics.get("identity_issues"):
        confidence -= 0.10  # a player wasn't found where the tap was: partner/opponent stats suspect
    if calibration and any(p[0] < 0 or p[1] < 0 for p in calibration.get("corners_px", [])):
        caveats.append("Some court corners were estimated off-frame — "
                       "positioning accuracy is reduced.")
        confidence -= 0.10
    if calibration and not calibration.get("kitchen_px"):
        caveats.append("Kitchen corners were not marked — kitchen-time accuracy "
                       "is lower; recalibrate with them for a better estimate.")
        confidence -= 0.05
    if metrics.get("camera_cuts", 0) > 3:
        confidence -= 0.15  # cuts fragment tracking + skew per-angle homography
    if metrics.get("secondary_court_tracks", 0) > 0:
        confidence -= 0.15  # likely doubles/crowd — opponent attribution unreliable
    if metrics.get("match_type") == "doubles":
        caveats.append("DUPR rubric anchors aren't tuned for doubles positioning "
                       "yet — treat this estimate as rougher than usual.")
        confidence -= 0.10
    if any("behind the baseline" in w for w in metrics.get("warnings", [])):
        confidence -= 0.25  # geometry broken: positioning dims untrustworthy
    confidence = round(float(np.clip(confidence, 0.05, 0.95)), 2)

    snapped = round(round(band / 0.25) * 0.25, 2)  # quarter-band granularity
    # "at the ceiling" = reads as the top band. (Not band >= 4.95: even flawless play scores
    # ~4.93 because shot_variety's top anchor is 4.5, so that test could never fire.)
    at_ceiling, at_floor = snapped >= BAND_MAX, snapped <= BAND_MIN
    if at_ceiling:
        caveats.append("This rubric tops out at about 5.0 — a stronger player would read the same.")
    calibrated = is_calibrated(metrics.get("match_type"))
    if not calibrated:
        caveats.append("Not yet calibrated against footage of players with known ratings — "
                       "a rough resemblance, not a measurement.")
    half = range_half_width(confidence)
    return {
        "available": True,
        "band": snapped,
        "band_raw": round(band, 2),
        "band_low": round(max(BAND_MIN, snapped - half), 2),
        "band_high": round(min(BAND_MAX, snapped + half), 2),
        "confidence": confidence,
        "confidence_level": confidence_level(confidence),
        "calibrated": calibrated,
        "at_ceiling": at_ceiling,
        "at_floor": at_floor,
        "dimensions": dims,
        "caveats": caveats,
    }
