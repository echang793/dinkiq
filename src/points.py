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

from court import on_court

OUT_MARGIN = 0.5  # ft tolerance around the boundary when calling a bounce "out"
BOUNCE_WINDOW_S = 2.0
MATCH_WINDOW_S = 0.25


def attribute_hitters(hit_times: np.ndarray, subject_swings: np.ndarray,
                      opponent_swings: np.ndarray) -> list[str]:
    """Per hit: 'subject' | 'opponent' | 'unknown', by nearest matching swing."""
    out = []
    for t in hit_times:
        ds = float(np.min(np.abs(subject_swings - t))) if len(subject_swings) else np.inf
        do = float(np.min(np.abs(opponent_swings - t))) if len(opponent_swings) else np.inf
        if ds > MATCH_WINDOW_S and do > MATCH_WINDOW_S:
            out.append("unknown")
        elif ds <= do:
            out.append("subject")
        else:
            out.append("opponent")
    return out


def rally_outcomes(rallies: list[dict], hit_times: np.ndarray, hitters: list[str],
                   bounces_court=None) -> list[dict]:
    """Per rally: server, last_hitter, winner, unforced_error (None if unknown)."""
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
        if last_hitter == "unknown":
            out.append({"server": server, "winner": "unknown", "unforced_error": None})
            continue
        other = "opponent" if last_hitter == "subject" else "subject"
        winner, unforced = last_hitter, False
        if bounces_court is not None and len(bounces_court):
            t0 = float(hit_times[last_i])
            near = bounces_court[(bounces_court["t"] > t0) &
                                 (bounces_court["t"] < t0 + BOUNCE_WINDOW_S)]
            if len(near):
                b = near.sort_values("t").iloc[0]
                if not on_court(float(b["x"]), float(b["y"]), margin=OUT_MARGIN):
                    winner, unforced = other, True
        out.append({"server": server, "winner": winner, "unforced_error": unforced})
    return out


def point_summary(outcomes: list[dict], ball_available: bool) -> dict:
    known = [o for o in outcomes if o["winner"] != "unknown"]
    won = sum(1 for o in known if o["winner"] == "subject")
    lost = sum(1 for o in known if o["winner"] == "opponent")
    served = [o for o in known if o["server"] == "subject"]
    returned = [o for o in known if o["server"] == "opponent"]

    def pct(sub: int, total: int) -> float | None:
        return round(100.0 * sub / total, 1) if total else None

    summary = {
        "points_scored": len(known),
        "points_won": won,
        "points_lost": lost,
        "win_pct": pct(won, len(known)),
        "serve_win_pct": pct(sum(1 for o in served if o["winner"] == "subject"), len(served)),
        "return_win_pct": pct(sum(1 for o in returned if o["winner"] == "subject"), len(returned)),
        "unforced_errors": sum(1 for o in outcomes if o["unforced_error"]),
    }
    if not ball_available:
        summary["caveat"] = ("Ball tracking unavailable — winners assume the last shot "
                             "of each rally was in; unforced errors are not detected.")
    return summary
