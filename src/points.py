"""Point-outcome detection: rally winners, serve/return win rates.

Heuristic, gated by available signal:
- the last hitter of a rally = whoever swung nearest in time to the rally's
  final audio hit (subject vs opponent wrist-speed peaks).
- where the ball first bounced after that final hit decides the point:
    * on the hitter's own side of the net  -> it never crossed: the hitter's error
    * on the far side but out of the court -> the hitter's error
    * on the far side and in               -> the hitter won it
    * nowhere seen, or within a foot of the net -> unknown. Never a guess.
- with no ball track at all the winner is unknown. The old rule assumed the last
  hitter won, but in recreational pickleball most rallies end on an error, so the
  last hitter is usually the one who LOST: that was a bias, not an approximation.
"""

import numpy as np

from court import BOUNCE_LOOKUP_WINDOW_S, NET_Y, first_bounce_after, on_court

OUT_MARGIN = 0.5  # ft tolerance around the boundary when calling a bounce "out"
NET_TOL_FT = 1.0  # a bounce this close to the net line can't be assigned to a side
BOUNCE_WINDOW_S = BOUNCE_LOOKUP_WINDOW_S
MATCH_WINDOW_S = 0.25


# Team membership for each possible hitter label — singles only ever
# populates "subject"/"opponent1"; doubles adds "partner"/"opponent2".
MY_TEAM = {"subject", "partner"}
OPP_TEAM = {"opponent1", "opponent2"}
TEAM_OF = {p: "my_team" for p in MY_TEAM} | {p: "opp_team" for p in OPP_TEAM}


def attribute_hitters(hit_times: np.ndarray,
                      player_swings: dict[str, np.ndarray]) -> list[str]:
    """Per hit: nearest player (by dict key, e.g. 'subject'/'partner'/
    'opponent1'/'opponent2') whose swing matches, or 'unknown'.

    Dict iteration order breaks exact-distance ties (first-listed wins) —
    singles callers pass {'subject':..., 'opponent1':...} to match the
    original subject-favored tiebreak.
    """
    out = []
    for t in hit_times:
        dists = [(float(np.min(np.abs(swings - t))) if len(swings) else np.inf, label)
                 for label, swings in player_swings.items()]
        best_d, best_label = min(dists, key=lambda d: d[0]) if dists else (np.inf, "unknown")
        out.append(best_label if best_d <= MATCH_WINDOW_S else "unknown")
    return out


def side_by_rally(rallies: list[dict], pos) -> list[int]:
    """+1 if my team plays the near side (y > net) during each rally, -1 the far side,
    0 when unknown. From the subject's own court positions around the rally -- players
    switch ends between games, so one side for the whole session is wrong."""
    if pos is None or not len(pos):
        return [0] * len(rallies)
    overall = float(np.median(pos["y"]))
    out = []
    for r in rallies:
        w = pos[(pos["t"] >= r["start"] - 1.0) & (pos["t"] <= r["end"] + 1.0)]
        y = float(w["y"].median()) if len(w) else overall
        out.append(int(np.sign(y - NET_Y)))
    return out


def rally_outcomes(rallies: list[dict], hit_times: np.ndarray, hitters: list[str],
                   bounces_court=None, my_side_by_rally: list[int] | None = None) -> list[dict]:
    """Per rally: server, last_hitter, winner, unforced_error (None if unknown).

    server / last_hitter are individual player labels; winner is always a team label
    ("my_team" / "opp_team") or "unknown". bounces_court=None means no usable ball
    track, so no winner can be called. my_side_by_rally (see side_by_rally) lets a
    shot into the net be recognised; without it only in/out is checked."""
    out = []
    for ri, r in enumerate(rallies):
        idx = [i for i, t in enumerate(hit_times)
               if r["start"] - 1e-6 <= t <= r["end"] + 1e-6]
        if not idx:
            out.append({"server": "unknown", "last_hitter": "unknown",
                       "winner": "unknown", "unforced_error": None})
            continue
        last_i = idx[-1]
        last_hitter = hitters[last_i]
        row = {"server": hitters[idx[0]], "last_hitter": last_hitter,
               "winner": "unknown", "unforced_error": None}
        if last_hitter not in TEAM_OF or bounces_court is None:
            out.append(row)
            continue
        team = TEAM_OF[last_hitter]
        other_team = "opp_team" if team == "my_team" else "my_team"
        b = first_bounce_after(bounces_court, float(hit_times[last_i]))
        if b is None:
            out.append(row)
            continue
        my_side = my_side_by_rally[ri] if my_side_by_rally and ri < len(my_side_by_rally) else 0
        hitter_side = my_side if team == "my_team" else -my_side
        if hitter_side:
            if abs(float(b["y"]) - NET_Y) <= NET_TOL_FT:
                out.append(row)             # at the net line: can't tell which side
                continue
            if np.sign(float(b["y"]) - NET_Y) == hitter_side:   # landed on the hitter's own side
                row.update(winner=other_team, unforced_error=True)
                out.append(row)
                continue
        if on_court(float(b["x"]), float(b["y"]), margin=OUT_MARGIN):
            row.update(winner=team, unforced_error=False)
        else:
            row.update(winner=other_team, unforced_error=True)
        out.append(row)
    return out


def point_summary(outcomes: list[dict], ball_available: bool,
                  hitters: list[str] | None = None) -> dict:
    """Team-level win/serve/return rates, plus a per-player hit-count
    breakdown (from the full per-hit `hitters` list) for individual
    attribution in the UI."""
    def team(label: str) -> str:
        return TEAM_OF.get(label, label)  # "my_team"/"opp_team", or already a team label from a flip

    known = [o for o in outcomes if o["winner"] != "unknown"]
    won = sum(1 for o in known if team(o["winner"]) == "my_team")
    lost = sum(1 for o in known if team(o["winner"]) == "opp_team")
    served = [o for o in known if team(o["server"]) == "my_team"]
    returned = [o for o in known if team(o["server"]) == "opp_team"]

    def pct(sub: int, total: int) -> float | None:
        return round(100.0 * sub / total, 1) if total else None

    hits_by_player: dict[str, int] = {}
    for label in (hitters or []):
        if label in TEAM_OF:
            hits_by_player[label] = hits_by_player.get(label, 0) + 1

    summary = {
        "points_scored": len(known),
        "points_won": won,
        "points_lost": lost,
        "win_pct": pct(won, len(known)),
        "serve_win_pct": pct(sum(1 for o in served if team(o["winner"]) == "my_team"), len(served)),
        "return_win_pct": pct(sum(1 for o in returned if team(o["winner"]) == "my_team"), len(returned)),
        "unforced_errors": sum(1 for o in outcomes if o["unforced_error"]),
        "hits_by_player": hits_by_player,
    }
    if not ball_available:
        summary["caveat"] = ("Ball tracking unavailable — who won each point can't be determined "
                             "(the last hitter usually lost it), so points, win rate and unforced "
                             "errors aren't scored.")
    return summary


def _win_rate_by_key(rows: list[tuple[str, str]]) -> dict:
    """(key, winner) pairs -> {key: {attempts, win_pct}}, "unknown" winners
    dropped. Shared tally shape for serve_side_win_rates/shot_type_outcomes."""
    tally: dict[str, dict[str, int]] = {}
    for key, winner in rows:
        if winner == "unknown":
            continue
        t = tally.setdefault(key, {"attempts": 0, "won": 0})
        t["attempts"] += 1
        if TEAM_OF.get(winner, winner) == "my_team":
            t["won"] += 1
    return {k: {"attempts": v["attempts"], "win_pct": round(100.0 * v["won"] / v["attempts"], 1)}
           for k, v in tally.items()}


def serve_side_win_rates(serves: list[dict], outcomes: list[dict]) -> dict:
    """Win rate by serve placement (left/middle/right).

    shots.json already records each subject serve's side and points.json
    already records each rally's winner, but the two are computed in
    separate pipeline stages and never cross-referenced. Joined here via
    each serve's rally_index (shots.serve_metrics) against the matching
    rally_outcomes entry -- e.g. "middle serves win 70% of points vs. 45%
    for wide serves" is a real pattern sitting in data the app already has.
    """
    rows = []
    for s in serves:
        ri = s.get("rally_index")
        if ri is None or ri >= len(outcomes):
            continue
        rows.append((s["side"], outcomes[ri]["winner"]))
    return _win_rate_by_key(rows)


def shot_type_outcomes(shots: list[dict], rallies: list[dict], outcomes: list[dict]) -> dict:
    """Win rate by shot type (drive/dink/drop/...), for rallies the subject
    personally ended.

    shots.json classifies every subject hit's type; points.json knows every
    rally's winner and (now) last_hitter -- joined here via each rally's end
    time, which is independently the same underlying hit_times value both
    shots.json's per-shot `t` and rallies[i]['end'] were rounded from, so the
    match is exact, not fuzzy. Only subject-ended rallies are scored: the app
    doesn't classify opponent/partner shot types, so extending this to "any
    rally" isn't supported by data that actually exists.
    """
    by_t = {s["t"]: s["type"] for s in shots}
    rows = []
    for r, o in zip(rallies, outcomes):
        if o.get("last_hitter") != "subject":
            continue
        shot_type = by_t.get(round(float(r["end"]), 2))
        if shot_type is not None:
            rows.append((shot_type, o["winner"]))
    return _win_rate_by_key(rows)
