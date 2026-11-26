"""Progress and practice: what changed since last session, goals, practice log, weekly plan."""

import datetime
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

import practice
from api.common import DEFAULT_PLAYER_NAME, belongs_to, compare_summary, get_session_dir, iter_sessions, read_artifact
from dupr import RUBRIC
from session_status import get_status

router = APIRouter()


def _when(d) -> tuple[str, float]:
    """Sort key: the day it was played (or uploaded), then upload time as the tiebreak."""
    meta = read_artifact(d, "meta.json")
    mtime = d.stat().st_mtime
    return (meta.get("played_at") or datetime.date.fromtimestamp(mtime).isoformat(), mtime)


def _who(meta: dict) -> str:
    return (meta.get("player") or DEFAULT_PLAYER_NAME).strip().casefold()


def _done_oldest_first():
    return sorted((d for d in iter_sessions(done_only=True)), key=_when)


def _latest_dims(player: str | None = None) -> dict:
    for d in reversed(_done_oldest_first()):
        if belongs_to(read_artifact(d, "meta.json"), player):
            dims = read_artifact(d, "dupr.json").get("dimensions")
            if dims:
                return dims
    return {}


@router.get("/api/session/{sid}/changes")
def changes(sid: str):
    """This session against the previous finished session of the same player: each headline metric
    and rubric dimension with its change and whether that is better or worse, whether the rating
    moved by more than the estimates' own margin, and a one-line headline."""
    sdir = get_session_dir(sid)
    if get_status(sdir).get("stage") != "done":
        raise HTTPException(409, "session not ready")
    me = _who(read_artifact(sdir, "meta.json"))
    key = _when(sdir)
    prev = next((d for d in reversed(_done_oldest_first())
                 if d.name != sid and _when(d) < key and _who(read_artifact(d, "meta.json")) == me), None)
    if prev is None:
        return {"available": False, "reason": "no earlier session to compare with"}
    now_d, prev_d = read_artifact(sdir, "dupr.json"), read_artifact(prev, "dupr.json")
    metrics = practice.compare_metrics(compare_summary(sdir), compare_summary(prev))
    rating = practice.rating_change(now_d, prev_d)
    label = compare_summary(prev)["label"]
    return {"available": True,
            "previous": {"session_id": prev.name, "label": label, "played_at": _when(prev)[0]},
            "metrics": metrics, "rating": rating,
            "dimensions": practice.compare_dimensions(now_d.get("dimensions"), prev_d.get("dimensions")),
            "headline": practice.headline(label, metrics, rating)}


# ── goals ───────────────────────────────────────────────────────────────

class GoalBody(BaseModel):
    target: float = Field(ge=0, le=1000)


def _check_dimension(dimension: str) -> None:
    if dimension not in RUBRIC:
        raise HTTPException(404, "unknown dimension")


@router.get("/api/goals")
def goals(player: str | None = None):
    store = practice.load_store()
    dims = _latest_dims(player)
    have = {g["dimension"] for g in store["goals"]}
    return {"goals": [practice.goal_view(g, (dims.get(g["dimension"]) or {}).get("value")) for g in store["goals"]],
            "suggested": practice.suggestions(dims, have),
            "dimensions": [{"dimension": k, "label": v["label"], "unit": v["unit"],
                            "higher_is_better": practice.higher_is_better(k)} for k, v in RUBRIC.items()]}


@router.put("/api/goals/{dimension}")
def put_goal(dimension: str, body: GoalBody):
    _check_dimension(dimension)
    store = practice.load_store()
    store["goals"] = [g for g in store["goals"] if g["dimension"] != dimension]
    store["goals"].append({"dimension": dimension, "target": body.target,
                           "created_at": datetime.date.today().isoformat()})
    practice.save_store(store)
    return {"ok": True}


@router.delete("/api/goals/{dimension}")
def delete_goal(dimension: str):
    store = practice.load_store()
    store["goals"] = [g for g in store["goals"] if g["dimension"] != dimension]
    practice.save_store(store)
    return {"ok": True}


# ── practice log ────────────────────────────────────────────────────────

class PracticeEntry(BaseModel):
    drill: str = Field(min_length=1, max_length=80)
    dimension: str | None = None
    minutes: int = Field(default=15, ge=1, le=600)
    date: str | None = None

    @field_validator("dimension")
    @classmethod
    def _known_dimension(cls, v):
        if v is not None and v not in RUBRIC:
            raise ValueError("unknown dimension")
        return v

    @field_validator("date")
    @classmethod
    def _valid_date(cls, v):
        if v is None:
            return v
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            raise ValueError("date must be YYYY-MM-DD")
        if datetime.date.fromisoformat(v) > datetime.date.today():
            raise ValueError("date can't be in the future")
        return v


@router.post("/api/practice")
def log_practice(entry: PracticeEntry):
    row = {"id": practice.new_id(), "drill": entry.drill.strip(), "dimension": entry.dimension,
           "minutes": entry.minutes, "date": entry.date or datetime.date.today().isoformat(),
           "created_at": datetime.datetime.now().timestamp()}
    store = practice.load_store()
    store["practice"] = practice.trim_practice(store["practice"] + [row])
    practice.save_store(store)
    return row


@router.get("/api/practice")
def list_practice(days: int = 90):
    since = (datetime.date.today() - datetime.timedelta(days=max(1, min(days, 3650)))).isoformat()
    rows = [p for p in practice.load_store()["practice"] if p["date"] >= since]
    return {"practice": sorted(rows, key=lambda p: (p["date"], p.get("created_at", 0)), reverse=True)}


@router.delete("/api/practice/{entry_id}")
def delete_practice(entry_id: str):
    store = practice.load_store()
    kept = [p for p in store["practice"] if p.get("id") != entry_id]
    if len(kept) == len(store["practice"]):
        raise HTTPException(404, "no such practice entry")
    store["practice"] = kept
    practice.save_store(store)
    return {"ok": True}


@router.get("/api/plan")
def plan(player: str | None = None):
    """This week's drills: the weakest dimensions over the last few sessions, ticked off by practice
    logged in the last 7 days."""
    recent = []
    for d in reversed(_done_oldest_first()):
        if belongs_to(read_artifact(d, "meta.json"), player):
            dims = read_artifact(d, "dupr.json").get("dimensions")
            if dims:
                recent.append(dims)
        if len(recent) >= practice.PLAN_SESSIONS:
            break
    return practice.build_plan(recent, practice.load_store()["practice"], datetime.date.today())
