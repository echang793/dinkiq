"""M4 DUPR-band rubric: metrics -> "your play resembles ~X.X" estimate.

Honest framing: this is a heuristic scorecard, not a certified rating. Each
dimension maps an observable metric to a band via piecewise-linear anchors;
the overall band is the weighted mean of available dimensions. Confidence
reflects how much of the evidence chain was actually measurable.

RUBRIC is the single source of truth — tune anchors/weights here only
(calibration against known-DUPR footage adjusts these numbers, not code).
"""

import json
from pathlib import Path

import numpy as np

BAND_MIN, BAND_MAX = 2.5, 5.0

# Each dimension: weight + (metric_value, band) anchor points, monotone in value.
RUBRIC: dict[str, dict] = {
    "nvz_discipline": {          # % of time at the kitchen line
        "weight": 0.30,
        "anchors": [(5, 2.5), (20, 3.0), (35, 3.5), (50, 4.25), (65, 5.0)],
        "label": "Kitchen presence",
    },
    "positioning": {             # % of time in no-man's land (lower = better)
        "weight": 0.20,
        "anchors": [(45, 2.5), (35, 3.0), (25, 3.5), (15, 4.25), (8, 5.0)],
        "label": "Court positioning",
    },
    "rally_sustain": {           # average hits per rally
        "weight": 0.20,
        "anchors": [(3, 2.5), (5, 3.0), (7, 3.5), (10, 4.25), (14, 5.0)],
        "label": "Rally consistency",
    },
    "shot_variety": {            # distinct shot types used (of drive/dink/drop)
        "weight": 0.15,
        "anchors": [(0, 2.5), (1, 3.0), (2, 3.75), (3, 4.5)],
        "label": "Shot variety",
    },
    "serve_depth": {             # % of measured serves landing within 8ft of baseline
        "weight": 0.15,
        "anchors": [(0, 2.75), (25, 3.0), (50, 3.5), (75, 4.25), (95, 5.0)],
        "label": "Serve depth",
    },
}


CALIBRATED_PATH = Path(__file__).resolve().parent.parent / "models" / "rubric_calibrated.json"


def active_anchors(name: str) -> list[tuple[float, float]]:
    """Anchors for a dimension: calibrated override if present, else RUBRIC.

    calibrate_rubric.py writes the override after fitting against sessions
    tagged with a known DUPR. RUBRIC stays the structural source of truth.
    """
    if CALIBRATED_PATH.exists():
        data = json.loads(CALIBRATED_PATH.read_text())
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
        vals["shot_variety"] = float(sum(1 for k in ("drive", "dink", "drop") if mix.get(k)))
        if shots.get("serves_measured", 0) >= 3:
            vals["serve_depth"] = float(shots["deep_serve_pct"])
    return vals


def estimate(metrics: dict, events: dict, shots: dict,
             calibration: dict | None = None) -> dict:
    vals = extract_dimension_values(metrics, events, shots)
    caveats: list[str] = list(metrics.get("warnings", []))

    dims = {}
    for name, spec in RUBRIC.items():
        if name in vals:
            dims[name] = {"label": spec["label"], "value": round(vals[name], 1),
                          "band": round(interp_band(vals[name], active_anchors(name)), 2),
                          "weight": spec["weight"]}

    if not dims or "nvz_discipline" not in dims:
        return {"available": False,
                "reason": "not enough measurable play to estimate a level",
                "caveats": caveats}

    wsum = sum(d["weight"] for d in dims.values())
    band = sum(d["band"] * d["weight"] for d in dims.values()) / wsum

    # confidence: starts from dimension coverage, docked for known accuracy risks
    confidence = 0.30 + 0.65 * (wsum / sum(s["weight"] for s in RUBRIC.values()))
    if metrics.get("active_seconds", 0) < 300:
        caveats.append("Short sample (<5 min of play) — estimate is volatile.")
        confidence -= 0.15
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
    if any("behind the baseline" in w for w in metrics.get("warnings", [])):
        confidence -= 0.25  # geometry broken: positioning dims untrustworthy
    confidence = round(float(np.clip(confidence, 0.05, 0.95)), 2)

    return {
        "available": True,
        "band": round(round(band / 0.25) * 0.25, 2),  # quarter-band granularity
        "band_raw": round(band, 2),
        "confidence": confidence,
        "dimensions": dims,
        "caveats": caveats,
    }
