"""M4 feedback engine: coaching tips keyed to the weakest rubric dimensions."""

TIPS: dict[str, dict] = {
    "nvz_discipline": {
        "low": "You spent only {value}% of your time at the kitchen line. Points are "
               "won there — drill 'return and run': after every return of serve, get "
               "all the way to the NVZ line before the third shot arrives.",
        "high": "Strong kitchen presence ({value}% of your time). Keep earning it "
                "with quality third shots.",
    },
    "positioning": {
        "low": "{value}% of your time was in no-man's land (8–15 ft from the net). "
               "Pick a home: kitchen line or baseline. Split-step when the ball is "
               "struck instead of drifting mid-court.",
        "high": "You rarely camp in no-man's land ({value}%) — good spacing.",
    },
    "rally_sustain": {
        "low": "Rallies averaged {value} hits — points are ending fast. Prioritize "
               "one more ball back: 80% pace, higher net clearance, middle target.",
        "high": "Rallies averaged {value} hits — you can hang in extended exchanges.",
    },
    "shot_variety": {
        "low": "Only {value:.0f} of drive/dink/drop showed up in your play. "
               "Add the missing shot — a third-shot drop if you always drive, "
               "a drive if you always dink — so opponents can't cheat forward.",
        "high": "You showed drive, dink, and drop — a full toolbox keeps opponents "
                "honest.",
    },
    "serve_depth": {
        "low": "Only {value}% of measured serves landed deep (within 8 ft of the "
               "baseline). Deep serves pin the returner back — aim 2 ft inside the "
               "baseline with margin over the net.",
        "high": "{value}% of your serves landed deep — that pressure sets up the "
                "whole point.",
    },
}

WEAK_BAND = 3.5   # dimensions below this band get improvement tips


def coach_tips(dupr: dict, max_tips: int = 3) -> list[str]:
    """Improvement tips for the weakest dimensions + one strength callout."""
    if not dupr.get("available"):
        return []
    dims = dupr["dimensions"]
    ranked = sorted(dims.items(), key=lambda kv: kv[1]["band"])
    tips = []
    for name, d in ranked:
        if d["band"] < WEAK_BAND and len(tips) < max_tips:
            tips.append(TIPS[name]["low"].format(value=d["value"]))
    best_name, best = ranked[-1]
    if best["band"] >= WEAK_BAND:
        tips.append(TIPS[best_name]["high"].format(value=best["value"]))
    return tips[:max_tips + 1]
