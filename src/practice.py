"""Progress over time: what changed since last session, goals, the weekly practice plan.

Pure logic plus one small store (goals.json: goals and the practice log). Single user, so there
are no accounts -- just the user's own file under the data dir.
"""

import datetime
import uuid

from dupr import RUBRIC
from feedback import DRILLS, WEAK_BAND
from jsonio import atomic_write_json, load_json
from pipeline import DATA

STORE = DATA / "goals.json"
MAX_PRACTICE_ENTRIES = 2000
PLAN_SESSIONS = 3          # recent sessions the plan averages over
PLAN_DAYS = 7
MAX_PLAN_ITEMS = 3
DIM_EPS_BAND = 0.1

# (key, label, unit, better direction: +1 higher / -1 lower / 0 neutral, smallest change worth naming)
METRICS = (
    ("kitchen_pct", "Time at the kitchen line", "%", 1, 2.0),
    ("transition_pct", "Time in no-man's land", "%", -1, 2.0),
    ("avg_rally_hits", "Average rally length", " hits", 1, 0.3),
    ("win_pct", "Win rate", "%", 1, 5.0),
    ("distance_ft", "Distance covered", " ft", 0, 50.0),
    ("avg_speed_ft_s", "Movement speed", " ft/s", 0, 0.3),
)


def higher_is_better(dimension: str) -> bool:
    a = RUBRIC[dimension]["anchors"]
    return a[0][0] < a[-1][0]


def _verdict(delta: float, eps: float, direction: int) -> str:
    if abs(delta) < eps:
        return "same"
    if direction == 0:
        return "neutral"
    return "better" if delta * direction > 0 else "worse"


def compare_metrics(now: dict, before: dict) -> list[dict]:
    out = []
    for key, label, unit, direction, eps in METRICS:
        a, b = now.get(key), before.get(key)
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            continue
        delta = round(a - b, 2)
        out.append({"key": key, "label": label, "unit": unit, "now": a, "before": b, "delta": delta,
                    "direction": _verdict(delta, eps, direction), "eps": eps})
    return out


def compare_dimensions(now: dict, before: dict) -> list[dict]:
    out = []
    for key, d in (now or {}).items():
        p = (before or {}).get(key)
        if not p or key not in RUBRIC:
            continue
        delta = round(d["band"] - p["band"], 2)
        out.append({"key": key, "label": d.get("label", key), "unit": d.get("unit", ""),
                    "now": d.get("value"), "before": p.get("value"),
                    "band_now": d["band"], "band_before": p["band"], "delta_band": delta,
                    "direction": _verdict(delta, DIM_EPS_BAND, 1)})
    return out


def rating_change(now: dict, before: dict) -> dict | None:
    """The estimate's movement, and whether it is bigger than the estimates' own uncertainty."""
    if now.get("band") is None or before.get("band") is None:
        return None

    def half_width(d):
        lo, hi = d.get("band_low"), d.get("band_high")
        return (hi - lo) / 2 if lo is not None and hi is not None else 0.0

    delta = round(now["band"] - before["band"], 2)
    margin = max(half_width(now), half_width(before))
    return {"now": now["band"], "before": before["band"], "delta": delta,
            "within_margin": abs(delta) <= margin}


def headline(prev_label: str, metrics: list[dict], rating: dict | None) -> str:
    moved = [m for m in metrics if m["direction"] in ("better", "worse")]
    def score(m: dict) -> float:   # the biggest move relative to what counts as one
        return abs(m["delta"]) / m["eps"]

    best = max((m for m in moved if m["direction"] == "better"), key=score, default=None)
    worst = max((m for m in moved if m["direction"] == "worse"), key=score, default=None)
    parts = []
    if best:
        parts.append(f"{best['label']} {best['delta']:+g}{best['unit']}")
    if worst:
        parts.append(f"{worst['label']} {worst['delta']:+g}{worst['unit']}")
    if rating and not rating["within_margin"]:
        parts.append(f"rating estimate {'up' if rating['delta'] > 0 else 'down'} {abs(rating['delta']):.2f}")
    if not parts:
        return f"About the same as {prev_label}."
    return f"Since {prev_label}: " + "; ".join(parts) + "."


# ── store ───────────────────────────────────────────────────────────────

def load_store() -> dict:
    data = load_json(STORE)
    data = data if isinstance(data, dict) else {}
    return {"goals": [g for g in data.get("goals", []) if isinstance(g, dict) and g.get("dimension") in RUBRIC],
            "practice": [p for p in data.get("practice", []) if isinstance(p, dict) and "date" in p]}


def save_store(store: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(STORE, store)


def new_id() -> str:
    return uuid.uuid4().hex[:8]


def trim_practice(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda p: (p["date"], p.get("created_at", 0)))[-MAX_PRACTICE_ENTRIES:]


# ── goals ───────────────────────────────────────────────────────────────

def goal_view(goal: dict, current: float | None) -> dict:
    dim, target = goal["dimension"], goal["target"]
    up = higher_is_better(dim)
    achieved = current is not None and (current >= target if up else current <= target)
    if current is None:
        pct = 0.0
    elif up:
        pct = 100.0 * current / target if target else 100.0
    else:
        pct = 100.0 * target / current if current else 100.0
    return {**goal, "label": RUBRIC[dim]["label"], "unit": RUBRIC[dim]["unit"], "higher_is_better": up,
            "current": current, "achieved": bool(achieved), "progress_pct": round(max(0.0, min(100.0, pct)), 1)}


def suggestions(dims: dict, have: set[str], limit: int = 3) -> list[dict]:
    """The next tier on each dimension the user has no goal for, weakest first."""
    ranked = sorted((d["band"], k, d) for k, d in (dims or {}).items() if k in RUBRIC and k not in have)
    return [{"dimension": k, "label": d.get("label", k), "unit": d.get("unit", ""),
             "current": d.get("value"), "target": d["next_target_value"]}
            for _, k, d in ranked if d.get("next_target_value") is not None][:limit]


# ── weekly plan ─────────────────────────────────────────────────────────

def week_of_practice(practice: list[dict], today: datetime.date) -> list[dict]:
    start = (today - datetime.timedelta(days=PLAN_DAYS - 1)).isoformat()
    return [p for p in practice if start <= p["date"] <= today.isoformat()]


def build_plan(dims_by_session: list[dict], practice: list[dict], today: datetime.date) -> dict:
    """Drills for the weakest dimensions averaged over the last few sessions, each ticked off once
    a matching practice entry exists this week. With nothing weak, the single weakest dimension
    is offered as maintenance rather than inventing a problem."""
    sessions = [d for d in dims_by_session if d][:PLAN_SESSIONS]
    week = week_of_practice(practice, today)
    base = {"week_minutes": sum(p.get("minutes", 0) for p in week), "sessions_considered": len(sessions)}
    if not sessions:
        return {"available": False, **base, "items": []}
    avg: dict[str, list[float]] = {}
    meta: dict[str, dict] = {}
    for dims in sessions:
        for k, d in dims.items():
            if k in RUBRIC and k in DRILLS:
                avg.setdefault(k, []).append(d["band"])
                meta[k] = d
    ranked = sorted((sum(v) / len(v), k) for k, v in avg.items())
    weak = [(b, k) for b, k in ranked if b < WEAK_BAND][:MAX_PLAN_ITEMS]
    chosen, maintenance = (weak, False) if weak else (ranked[:1], True)
    items = []
    for band, k in chosen:
        drill = DRILLS[k]
        logged = [p for p in week if p.get("dimension") == k or p.get("drill") == drill["name"]]
        items.append({"dimension": k, "label": meta[k].get("label", k), "average_band": round(band, 2),
                      "drill": drill, "maintenance": maintenance, "logged_this_week": len(logged),
                      "done": bool(logged)})
    return {"available": bool(items), **base, "items": items}
