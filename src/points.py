"""Point-outcome detection: rally winners, serve/return win rates.

Heuristic, gated by available signal:
- the last hitter of a rally = whoever swung nearest in time to the rally's
  final audio hit (subject vs opponent wrist-speed peaks).
- when ball tracking is available, a bounce landing outside the court just
  after that final hit flips the winner to the OTHER player (the hitter made
  an unforced error); without ball tracking we assume the last shot was a
  winner — callers should surface that assumption (see point_summary caveat).
"""

import numpy as np

from court import BOUNCE_LOOKUP_WINDOW_S, on_court

OUT_MARGIN = 0.5  # ft tolerance around the boundary when calling a bounce "out"
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


def rally_outcomes(rallies: list[dict], hit_times: np.ndarray, hitters: list[str],
                   bounces_court=None) -> list[dict]:
    """Per rally: server, last_hitter, winner, unforced_error (None if unknown).

    server/last_hitter/winner are individual player labels; team membership
    (TEAM_OF) determines who the "other side" is for the unforced-error flip,
    so this generalizes unchanged from 2-player singles to 4-player doubles.
    """
    out = []
    for r in rallies:
        idx = [i for i, t in enumerate(hit_times)
               if r["start"] - 1e-6 <= t <= r["end"] + 1e-6]
        if not idx:
            out.append({"server": "unknown", "winner": "unknown", "unforced_error": None})
            continue
        server = hitters[idx[0]]
        last_i = idx[-1]
        last_hitter = hitters[last_i]
        if last_hitter == "unknown" or last_hitter not in TEAM_OF:
            out.append({"server": server, "winner": "unknown", "unforced_error": None})
            continue
        other_team = "opp_team" if TEAM_OF[last_hitter] == "my_team" else "my_team"
        winner, unforced = last_hitter, False
        if bounces_court is not None and len(bounces_court):
            t0 = float(hit_times[last_i])
            near = bounces_court[(bounces_court["t"] > t0) &
                                 (bounces_court["t"] < t0 + BOUNCE_WINDOW_S)]
            if len(near):
                b = near.sort_values("t").iloc[0]
                if not on_court(float(b["x"]), float(b["y"]), margin=OUT_MARGIN):
                    winner, unforced = other_team, True
        out.append({"server": server, "winner": winner, "unforced_error": unforced})
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
        summary["caveat"] = ("Ball tracking unavailable — winners assume the last shot "
                             "of each rally was in; unforced errors are not detected.")
    return summary
