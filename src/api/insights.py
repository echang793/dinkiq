"""Cross-session analytics: progress, comparisons, opponents, streak, leaderboard, partner synergy."""

import datetime

from fastapi import APIRouter, HTTPException

import pipeline
from api.common import DEFAULT_PLAYER_NAME, compare_summary, get_session_dir, read_artifact, iter_sessions
from jsonio import load_json
from session_status import get_status

router = APIRouter()


NON_METRIC_FIELDS = {"session_id", "label", "played_at", "opponent"}


@router.get("/api/progress")
def progress():
    """Cross-session trend series (completed sessions, oldest first)."""
    points = []
    for d in iter_sessions(done_only=True):
        m_f, e_f, dp_f, meta_f = (d / "metrics.json", d / "events.json",
                                  d / "dupr.json", d / "meta.json")
        m = load_json(m_f)
        if not m:  # missing or unreadable: nothing to chart for this session
            continue
        ev = load_json(e_f)
        dp = load_json(dp_f)
        meta = load_json(meta_f)
        date = meta.get("played_at") or datetime.date.fromtimestamp(
            d.stat().st_mtime).isoformat()
        drills = dp.get("drills") or []
        top_drill = drills[0] if drills else {}
        points.append({
            "session_id": d.name,
            "label": meta.get("label") or meta.get("filename"),
            "date": date,
            "kitchen_pct": m.get("zone_pct", {}).get("kitchen"),
            "transition_pct": m.get("zone_pct", {}).get("transition"),
            "distance_ft": m.get("distance_ft"),
            "avg_rally_hits": ev.get("avg_rally_hits"),
            "dupr_band": dp.get("band"),
            "dupr_confidence": dp.get("confidence"),
            "known_dupr": meta.get("known_dupr"),
            "weakest_dimension": top_drill.get("dimension"),
            "weakest_label": top_drill.get("target_label"),
            # per-dimension bands, so the UI can tell whether a
            # previously-flagged weak dimension has actually improved
            "dimensions": dp.get("dimensions") or {},
        })
    points.sort(key=lambda p: p["date"])
    return points


@router.get("/api/compare")
def compare(a: str, b: str):
    da, db = get_session_dir(a), get_session_dir(b)
    for d in (da, db):
        if get_status(d).get("stage") != "done":
            raise HTTPException(409, f"session {d.name} is not ready to compare")
    sa, sb = compare_summary(da), compare_summary(db)
    diffs = {}
    for k in sa:
        if k in NON_METRIC_FIELDS:
            continue
        va, vb = sa.get(k), sb.get(k)
        delta = round(vb - va, 2) if isinstance(va, (int, float)) and isinstance(vb, (int, float)) else None
        diffs[k] = {"a": va, "b": vb, "delta": delta}
    return {"a": sa, "b": sb, "diffs": diffs}


@router.get("/api/compare/multi")
def compare_multi(ids: str):
    """Compare 3+ sessions side by side (no pairwise deltas — just summaries).

    ids is a comma-separated list of session ids, e.g. ?ids=a,b,c.
    """
    sids = [s for s in ids.split(",") if s]
    if len(sids) < 2:
        raise HTTPException(400, "provide at least 2 session ids")
    dirs = [get_session_dir(s) for s in sids]
    for d in dirs:
        if get_status(d).get("stage") != "done":
            raise HTTPException(409, f"session {d.name} is not ready to compare")
    return {"sessions": [compare_summary(d) for d in dirs]}


@router.get("/api/opponents/{name}/history")
def opponent_history(name: str):
    """Aggregate win/loss record + per-session summaries against one named
    opponent (case-insensitive exact match on the session's `opponent`
    meta field), newest first."""
    sessions_out = []
    for d in iter_sessions(newest_first=True):
        meta = read_artifact(d, "meta.json")
        if (meta.get("opponent") or "").strip().lower() != name.strip().lower():
            continue
        if get_status(d).get("stage") != "done":
            continue
        sessions_out.append(compare_summary(d))
    known = [s for s in sessions_out if s.get("win_pct") is not None]
    wins = sum(1 for s in known if s["points_won"] > s["points_lost"])
    losses = sum(1 for s in known if s["points_lost"] > s["points_won"])
    return {
        "opponent": name,
        "matches": len(sessions_out),
        "wins": wins,
        "losses": losses,
        "win_pct": round(100.0 * wins / len(known), 1) if known else None,
        "sessions": sessions_out,
    }


@router.get("/api/opponents/{name}/scouting")
def opponent_scouting(name: str):
    """Aggregate this opponent's own shot-landing tendency (dominant side,
    from tracked bounces after their hits — see shots.py's opponent_shots)
    across every completed session played against them, plus the win/loss
    context from opponent_history. Real-data only: sessions with no ball
    tracking simply don't contribute landing data."""
    side_counts: dict[str, int] = {}
    shots_tracked = 0
    for d in iter_sessions():
        meta = read_artifact(d, "meta.json")
        if (meta.get("opponent") or "").strip().lower() != name.strip().lower():
            continue
        if get_status(d).get("stage") != "done":
            continue
        opp = read_artifact(d, "shots.json").get("opponent_shots") or {}
        for side, n in (opp.get("side_counts") or {}).items():
            side_counts[side] = side_counts.get(side, 0) + n
        shots_tracked += opp.get("shots_tracked", 0)

    record = opponent_history(name)
    dominant = max(side_counts, key=side_counts.get) if side_counts else None
    return {
        "opponent": name,
        "matches": record["matches"],
        "wins": record["wins"],
        "losses": record["losses"],
        "win_pct": record["win_pct"],
        "shots_tracked": shots_tracked,
        "side_counts": side_counts,
        "dominant_side": dominant,
        "dominant_side_pct": round(100.0 * side_counts[dominant] / shots_tracked, 1)
                            if dominant and shots_tracked else None,
    }


@router.get("/api/streak")
def streak():
    """Consecutive-day play streak + this-week count, scoped to
    DEFAULT_PLAYER_NAME sessions (same "your" scoping as the leaderboard's
    unset-player bucket) — a session tagged with someone else's `player`
    shouldn't inflate your own streak."""
    dates: set[datetime.date] = set()
    for d in iter_sessions(done_only=True):
        meta = read_artifact(d, "meta.json")
        if (meta.get("player") or "").strip() and meta["player"].strip() != DEFAULT_PLAYER_NAME:
            continue
        played_at = meta.get("played_at")
        if played_at:
            try:
                dates.add(datetime.date.fromisoformat(played_at))
                continue
            except ValueError:
                pass
        dates.add(datetime.date.fromtimestamp(d.stat().st_mtime))

    today = datetime.date.today()
    one_day = datetime.timedelta(days=1)

    def run_forward_from(start: datetime.date) -> int:
        n, cur = 0, start
        while cur in dates:
            n += 1
            cur += one_day
        return n

    def run_back_from(start: datetime.date) -> int:
        n, cur = 0, start
        while cur in dates:
            n += 1
            cur -= one_day
        return n

    current = run_back_from(today) or run_back_from(today - one_day)
    longest = max((run_forward_from(dt) for dt in dates if dt - one_day not in dates), default=0)
    week_ago = today - datetime.timedelta(days=6)
    sessions_this_week = sum(1 for dt in dates if week_ago <= dt <= today)

    return {
        "current_streak_days": current,
        "longest_streak_days": longest,
        "sessions_this_week": sessions_this_week,
        "days_played": len(dates),
    }


@router.get("/api/leaderboard")
def leaderboard():
    """Rank players — whoever's footage a session belongs to, via the
    `player` meta field — by latest skill estimate, aggregated across all
    of their completed sessions. Sessions with no `player` set (everything
    that predates this field) fall under DEFAULT_PLAYER_NAME, so a
    single-user history still shows up as one row."""
    by_player: dict[str, list[dict]] = {}
    for d in iter_sessions(newest_first=False, done_only=True):
        meta = read_artifact(d, "meta.json")
        name = (meta.get("player") or "").strip() or DEFAULT_PLAYER_NAME
        by_player.setdefault(name, []).append(compare_summary(d))

    rows = []
    for name, sessions in by_player.items():
        bands = [s["dupr_band"] for s in sessions if s["dupr_band"] is not None]
        won = sum(s["points_won"] or 0 for s in sessions)
        lost = sum(s["points_lost"] or 0 for s in sessions)
        rows.append({
            "player": name,
            "sessions": len(sessions),
            "latest_band": bands[-1] if bands else None,
            "best_band": max(bands) if bands else None,
            "win_pct": round(100.0 * won / (won + lost), 1) if (won + lost) else None,
            "total_distance_ft": round(sum(s["distance_ft"] or 0 for s in sessions), 1),
        })
    rows.sort(key=lambda r: (r["latest_band"] is None, -(r["latest_band"] or 0)))
    return {"players": rows}


@router.get("/api/session/{sid}/synergy")
def synergy(sid: str):
    """Doubles partner positioning report. Computed on demand from
    tracks.parquet + calibration.json (both survive recalibration) rather
    than stored at analyze time, since it's cheap and only needed when a
    user opens it."""
    sdir = get_session_dir(sid)
    if get_status(sdir).get("stage") != "done":
        raise HTTPException(409, "session not ready")
    m = read_artifact(sdir, "metrics.json")
    if m.get("match_type") != "doubles" or m.get("partner_track_id") is None:
        return {"available": False, "reason": "singles session — no partner to compare"}
    if m.get("partner_track_id") == m.get("subject_track_id"):
        return {"available": False,
                "reason": "partner tap resolved to you — recalibrate and tap your partner to get a partner report"}
    tracks_f, calib_f = sdir / "tracks.parquet", sdir / "calibration.json"
    if not tracks_f.exists() or not calib_f.exists():
        return {"available": False, "reason": "tracking data not available"}

    import pandas as pd

    from court import CourtCalibration
    from feedback import synergy_tip
    from metrics import synergy_report
    from tracking import subject_court_positions

    tracks = pd.read_parquet(tracks_f)
    calib_data = load_json(calib_f)
    if not calib_data.get("corners_px"):
        return {"available": False, "reason": "calibration data unreadable"}
    calib = CourtCalibration(calib_data["corners_px"], calib_data.get("kitchen_px"))
    partner_pos = subject_court_positions(tracks, m["partner_track_id"], calib, pipeline.FPS)
    pos_f = sdir / "positions.parquet"
    subject_pos = pd.read_parquet(pos_f) if pos_f.exists() else pd.DataFrame(columns=["t", "x", "y"])
    report = synergy_report(subject_pos, partner_pos)
    report["tip"] = synergy_tip(report)
    return report
