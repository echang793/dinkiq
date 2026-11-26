"""Analytics and "show me the clips" lookups, derived from artifacts the pipeline already wrote.

No new tracking: everything is computed from events / hits / points / shots / positions at
request time, so it applies to old sessions without a reprocess. Each metric carries the sample
size it rests on and says `available: False` below a floor, because a percentage over two
points is noise and the footage here is often too poor for ball-based numbers at all.
"""

import numpy as np
import pandas as pd

from court import NET_Y, NVZ_DEPTH, zone_for
from feedback import DRILLS
from points import MY_TEAM, TEAM_OF

MIN_SCORED = 4          # scored rallies before a win rate is shown
MIN_RETURNS = 3
MIN_THIRD_SHOTS = 2
MIN_ERRORS_FOR_TIP = 3
MIN_END_ZONE = 4
BREAK_S = 60.0          # a gap longer than this between rallies is a break, not rest between points
RETURN_WINDOW_S = 8.0   # how long after a return we look for you reaching the kitchen
BUCKETS = (("short", "1–3 hits", 1, 3), ("medium", "4–6 hits", 4, 6), ("long", "7+ hits", 7, 10**6))
SHOT_TYPES = ("drive", "dink", "drop", "medium")
MOMENT_KINDS = ("won", "lost", "unforced_errors", "longest", "serves", *SHOT_TYPES)


def _pct(part: int, whole: int) -> float | None:
    return round(100.0 * part / whole, 1) if whole else None


def _team(label: str | None) -> str | None:
    return TEAM_OF.get(label or "", label)


def _scored(events: dict, points: dict) -> list[tuple[int, dict, dict, str]]:
    """(index, rally, outcome, winning team) for every rally whose winner is known."""
    out = []
    for i, (r, o) in enumerate(zip(events.get("rallies") or [], points.get("outcomes") or [])):
        w = _team(o.get("winner"))
        if w in ("my_team", "opp_team"):
            out.append((i, r, o, w))
    return out


def _by_rally_length(events: dict, points: dict) -> dict:
    scored = _scored(events, points)
    buckets = []
    for key, label, lo, hi in BUCKETS:
        inb = [(r, w) for _, r, _, w in scored if lo <= (r.get("hits") or 0) <= hi]
        won = sum(1 for _, w in inb if w == "my_team")
        buckets.append({"key": key, "label": label, "played": len(inb), "won": won,
                        "win_pct": _pct(won, len(inb))})
    return {"available": len(scored) >= MIN_SCORED, "scored": len(scored), "buckets": buckets}


def _pace(events: dict) -> dict:
    rallies = events.get("rallies") or []
    if len(rallies) < 3:
        return {"available": False}
    gaps = [b["start"] - a["end"] for a, b in zip(rallies, rallies[1:])]
    rests = [g for g in gaps if 0 <= g <= BREAK_S]
    if not rests:
        return {"available": False}
    work = sum(r["end"] - r["start"] for r in rallies)
    return {"available": True, "rallies": len(rallies),
            "rallies_per_min": round(len(rallies) / ((work + sum(rests)) / 60.0), 2),
            "avg_rest_s": round(float(np.mean(rests)), 2),
            "work_rest_ratio": round(work / sum(rests), 2),
            "breaks": sum(1 for g in gaps if g > BREAK_S)}


def _hit_index(hits: list[dict]) -> dict[tuple[int, int], dict]:
    return {(h["rally"], h["n"]): h for h in hits
            if isinstance(h, dict) and h.get("rally") is not None and h.get("n") is not None}


def _return_and_run(outcomes: list[dict], hits: list[dict], positions: pd.DataFrame | None) -> dict:
    if positions is None or not len(positions):
        return {"available": False, "reason": "needs your court positions"}
    idx = _hit_index(hits)
    t, y = positions["t"].to_numpy(), positions["y"].to_numpy()
    at_net = np.abs(y - NET_Y) <= NVZ_DEPTH + 1.0       # the same band zone_for() calls "kitchen"
    returns, secs = 0, []
    for i, o in enumerate(outcomes):
        h = idx.get((i, 1))
        if _team(o.get("server")) != "opp_team" or not h or h.get("hitter") != "subject":
            continue
        returns += 1
        window = (t >= h["t"]) & (t <= h["t"] + RETURN_WINDOW_S) & at_net
        if window.any():
            secs.append(float(t[window][0] - h["t"]))
    return {"available": returns >= MIN_RETURNS, "returns": returns, "reached": len(secs),
            "reached_pct": _pct(len(secs), returns),
            "avg_secs_to_kitchen": round(float(np.mean(secs)), 1) if secs else None}


def _third_shot(outcomes: list[dict], hits: list[dict], shots: dict) -> dict:
    if not shots.get("available"):
        return {"available": False, "reason": "needs a usable ball track"}
    idx = _hit_index(hits)
    typed = shots.get("shots") or []
    mix: dict[str, int] = {}
    for i, o in enumerate(outcomes):
        h = idx.get((i, 2))
        if _team(o.get("server")) != "my_team" or not h or h.get("hitter") != "subject":
            continue
        s = next((s for s in typed if abs(s["t"] - h["t"]) <= 0.05), None)
        if s:
            mix[s["type"]] = mix.get(s["type"], 0) + 1
    n = sum(mix.values())
    return {"available": n >= MIN_THIRD_SHOTS, "n": n, "mix": mix}


def _errors(outcomes: list[dict]) -> dict:
    """Your side's unforced errors by where the ball went. `unknown` = analyses from before the
    direction was recorded."""
    counts = {"net": 0, "long": 0, "wide": 0, "unknown": 0}
    for o in outcomes:
        if o.get("unforced_error") and o.get("last_hitter") in MY_TEAM and _team(o.get("winner")) == "opp_team":
            k = o.get("error_kind")
            counts[k if k in counts else "unknown"] += 1
    return {**counts, "total": sum(counts.values())}


def _end_zone(events: dict, points: dict, positions: pd.DataFrame | None) -> dict:
    if positions is None or not len(positions):
        return {"available": False}
    t = positions["t"].to_numpy()
    zones: dict[str, dict] = {}
    for _, r, _, w in _scored(events, points):
        j = int(np.argmin(np.abs(t - r["end"])))
        if abs(t[j] - r["end"]) > 1.5:
            continue
        z = zones.setdefault(zone_for(float(positions["y"].iloc[j])), {"played": 0, "won": 0})
        z["played"] += 1
        z["won"] += w == "my_team"
    for z in zones.values():
        z["win_pct"] = _pct(z["won"], z["played"])
    return {"available": sum(z["played"] for z in zones.values()) >= MIN_END_ZONE, "zones": zones}


_ERROR_ADVICE = {
    "net": "went into the net — aim a few feet higher over it, especially on dinks and drops.",
    "long": "sailed long — take some pace off and aim for the middle of the court.",
    "wide": "went wide — aim crosscourt-middle and keep more margin from the sideline.",
}


def _tips(errors: dict, third: dict, rar: dict) -> list[str]:
    tips = []
    known = errors["net"] + errors["long"] + errors["wide"]
    if known >= MIN_ERRORS_FOR_TIP:
        kind = max(_ERROR_ADVICE, key=lambda k: errors[k])
        if errors[kind] / known >= 0.5:
            tips.append(f"{errors[kind]} of {known} of your errors {_ERROR_ADVICE[kind]}")
    if third.get("available") and third["n"] >= 4 and third["mix"].get("drive", 0) / third["n"] >= 0.7:
        tips.append(f"{third['mix']['drive']} of your {third['n']} third shots were drives. "
                    "Mix in the third-shot drop so the returners can't crowd the net.")
    if rar.get("available") and rar["avg_secs_to_kitchen"] is not None and rar["avg_secs_to_kitchen"] > 4.0:
        tips.append(f"After a return you took about {rar['avg_secs_to_kitchen']} s to reach the kitchen "
                    f"line. Try {DRILLS['nvz_discipline']['name']}: move forward the moment you make contact.")
    elif rar.get("available") and rar["reached_pct"] is not None and rar["reached_pct"] < 50:
        tips.append(f"You reached the kitchen after only {rar['reached']} of {rar['returns']} returns. "
                    f"{DRILLS['nvz_discipline']['name']} builds the habit.")
    return tips


def extras(events: dict, hits: list[dict], points: dict, shots: dict,
           positions: pd.DataFrame | None) -> dict:
    outcomes = points.get("outcomes") or []
    errors = _errors(outcomes)
    third = _third_shot(outcomes, hits, shots)
    rar = _return_and_run(outcomes, hits, positions)
    return {"by_rally_length": _by_rally_length(events, points), "pace": _pace(events),
            "return_and_run": rar, "third_shot": third, "errors": errors,
            "end_zone": _end_zone(events, points, positions),
            "tips": _tips(errors, third, rar)}


# ── moments: where in the video a number comes from ─────────────────────

def _clock(s: float) -> str:
    return f"{int(s // 60)}:{int(s % 60):02d}"


def _rally_moment(i: int, r: dict, extra: str = "") -> dict:
    return {"t": round(float(r["start"]), 2), "rally": i,
            "start": round(max(0.0, r["start"] - 1.5), 2), "end": round(float(r["end"]) + 1.5, 2),
            "label": f"Rally {i + 1} · {r.get('hits', '?')} hits" + extra}


def _shot_moment(t: float, rallies: list[dict], label: str, lead: float = 2.0, tail: float = 3.0) -> dict:
    ri = next((i for i, r in enumerate(rallies) if r["start"] - 0.01 <= t <= r["end"] + 2.0), None)
    return {"t": round(float(t), 2), "rally": ri, "start": round(max(0.0, t - lead), 2),
            "end": round(float(t) + tail, 2), "label": f"{label} at {_clock(t)}"}


def moments(events: dict, points: dict, shots: dict) -> dict[str, list[dict]]:
    """Seekable windows into the full video behind each headline number, by kind."""
    rallies = events.get("rallies") or []
    out: dict[str, list[dict]] = {k: [] for k in MOMENT_KINDS}
    for i, (r, o) in enumerate(zip(rallies, points.get("outcomes") or [])):
        w = _team(o.get("winner"))
        if w == "my_team":
            out["won"].append(_rally_moment(i, r))
        elif w == "opp_team":
            out["lost"].append(_rally_moment(i, r))
        if o.get("unforced_error"):
            who = "your error" if o.get("last_hitter") in MY_TEAM else "opponent's error"
            out["unforced_errors"].append(_rally_moment(i, r, f" · {who}"))
    if rallies:
        i = max(range(len(rallies)), key=lambda k: rallies[k].get("hits") or 0)
        out["longest"].append(_rally_moment(i, rallies[i]))
    for s in shots.get("shots") or []:
        if s.get("type") in SHOT_TYPES:
            out[s["type"]].append(_shot_moment(s["t"], rallies, s["type"].capitalize()))
    for s in shots.get("serves") or []:
        m = _shot_moment(s["t"], rallies, "Serve", lead=1.0, tail=4.0)
        m["rally"] = s.get("rally_index", m["rally"])
        out["serves"].append(m)
    return out
