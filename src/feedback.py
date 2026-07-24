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

DRILLS: dict[str, dict] = {
    "nvz_discipline": {
        "name": "Return-and-Run",
        "reps": "10 reps",
        "description": "After every return of serve, sprint all the way to the NVZ "
                       "line before the third shot arrives — no stopping short.",
    },
    "positioning": {
        "name": "Split-Step Transition",
        "reps": "15 reps",
        "description": "Start at the baseline, split-step the moment the ball is "
                       "struck, advance one controlled step at a time — never park "
                       "in no-man's land.",
    },
    "rally_sustain": {
        "name": "10-Ball Rally Target",
        "reps": "5 rallies",
        "description": "Dink cross-court at 80% pace with high net clearance, aiming "
                       "for 10+ consecutive shots before either side goes for a "
                       "putaway.",
    },
    "shot_variety": {
        "name": "Drive-Drop-Dink Ladder",
        "reps": "3 rounds of 5",
        "description": "Off a fed ball, cycle drive, third-shot drop, then dink in "
                       "sequence — build all three into the same rally instead of "
                       "defaulting to one.",
    },
    "serve_depth": {
        "name": "Deep-Serve Targets",
        "reps": "20 serves",
        "description": "Place a target 2 ft inside the baseline and serve until you "
                       "land 15 of 20 inside it, with margin over the net.",
    },
}


def drills_for_weak(dupr: dict, max_drills: int = 3) -> list[dict]:
    """A drill for every weak dimension, ranked worst-first — matches the
    same weak-dimension set coach_tips() surfaces, so the tips a player
    reads and the practice plan they're given actually agree. Empty list
    if nothing is weak enough to warrant one."""
    if not dupr.get("available"):
        return []
    dims = dupr.get("dimensions") or {}
    if not dims:
        return []
    ranked = sorted(dims.items(), key=lambda kv: kv[1]["band"])
    drills = []
    for name, d in ranked:
        if d["band"] >= WEAK_BAND or len(drills) >= max_drills:
            break
        drill = dict(DRILLS[name])
        drill["dimension"] = name
        drill["target_label"] = d["label"]
        drill["band"] = d["band"]
        drills.append(drill)
    return drills


FATIGUE_DROP_PCT = 20.0  # meaningful conditioning-related decline, not noise


def fatigue_note(movement_curve: list[dict]) -> str | None:
    """A coaching sentence when movement speed fades meaningfully in the
    second half of a session — the movement_curve chart already shows this,
    but a chart is easy to skim past; say it in words. None if the sample
    is too short to trust or there's no meaningful drop."""
    if len(movement_curve) < 4:
        return None
    half = len(movement_curve) // 2
    early = [b["avg_speed_ft_s"] for b in movement_curve[:half]]
    late = [b["avg_speed_ft_s"] for b in movement_curve[half:]]
    early_avg = sum(early) / len(early)
    late_avg = sum(late) / len(late)
    if early_avg <= 0:
        return None
    drop_pct = 100.0 * (early_avg - late_avg) / early_avg
    if drop_pct < FATIGUE_DROP_PCT:
        return None
    return (f"Your movement speed dropped {drop_pct:.0f}% in the second half of the "
           f"session ({early_avg:.1f} → {late_avg:.1f} ft/s) — conditioning, not "
           "technique, may be the limiter late in matches.")


SYNERGY_HIGH_OVERLAP_PCT = 35.0   # heuristic doubles-positioning thresholds --
SYNERGY_LOW_SEPARATION_FT = 8.0   # not calibrated against real match data (same
SYNERGY_HIGH_SEPARATION_FT = 16.0 # caveat dupr.py already carries for doubles positioning)


def synergy_tip(synergy: dict) -> str | None:
    """One coaching sentence from a doubles synergy report (metrics.synergy_
    report's separation/overlap numbers), or None if nothing stands out.
    Those numbers currently only ever reach the UI as raw stats with no
    prescription, unlike the singles rubric's coach_tips/drills_for_weak."""
    if not synergy.get("available"):
        return None
    overlap = synergy.get("coverage_overlap_pct")
    sep = synergy.get("avg_separation_ft")
    if overlap is not None and overlap >= SYNERGY_HIGH_OVERLAP_PCT:
        return (f"You and your partner covered the same court area {overlap:.0f}% of the "
               "time — you're bunching up. Spread out so one of you isn't defending "
               "ground the other already has.")
    if sep is not None and sep <= SYNERGY_LOW_SEPARATION_FT:
        return (f"You averaged only {sep:.1f} ft apart — that's tight for a 20 ft-wide "
               "court. Widen your spacing so a ball down the line doesn't split the gap "
               "between you.")
    if sep is not None and sep >= SYNERGY_HIGH_SEPARATION_FT:
        return (f"You averaged {sep:.1f} ft apart — that's wide enough to leave the "
               "middle open. Tighten up so neither of you is chasing a ball that lands "
               "between you.")
    return None


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
