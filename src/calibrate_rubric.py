"""Fit DUPR rubric anchors against sessions with a known DUPR rating.

Workflow: upload + calibrate each known-rating clip in the app, set its
"Known DUPR" in Session info, then run:

    python3 src/calibrate_rubric.py            # fit + report
    python3 src/calibrate_rubric.py --dry-run  # report only, don't write

Fitting: per-dimension band SHIFT (piecewise anchors slide up/down together),
chosen by exhaustive grid search to minimize MAE of the weighted rubric
estimate vs known ratings. Deliberately low-parameter (one shift per
dimension) so a handful of clips can't overfit. Writes
models/rubric_calibrated.json, which dupr.active_anchors() picks up.
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dupr import BAND_MAX, BAND_MIN, RUBRIC, CALIBRATED_PATH, extract_dimension_values, interp_band

SESSIONS = Path(__file__).resolve().parent.parent / "data" / "sessions"
MIN_SESSIONS = 4
SHIFTS = np.arange(-0.75, 0.80, 0.25)  # per-dimension band shift search space


def load_labeled() -> list[dict]:
    out = []
    for d in sorted(SESSIONS.iterdir() if SESSIONS.exists() else []):
        meta_f, m_f, e_f, s_f = (d / "meta.json", d / "metrics.json",
                                 d / "events.json", d / "shots.json")
        if not (meta_f.exists() and m_f.exists()):
            continue
        meta = json.loads(meta_f.read_text())
        if meta.get("known_dupr") is None:
            continue
        vals = extract_dimension_values(
            json.loads(m_f.read_text()),
            json.loads(e_f.read_text()) if e_f.exists() else {},
            json.loads(s_f.read_text()) if s_f.exists() else {})
        if "nvz_discipline" not in vals:
            continue
        out.append({"session": d.name, "label": meta.get("label") or meta.get("filename"),
                    "known": float(meta["known_dupr"]), "vals": vals})
    return out


def predict(vals: dict, shifts: dict[str, float]) -> float:
    """Weighted rubric estimate with per-dimension anchor shifts applied."""
    total_w, acc = 0.0, 0.0
    for name, spec in RUBRIC.items():
        if name not in vals:
            continue
        anchors = [(v, float(np.clip(b + shifts.get(name, 0.0), BAND_MIN, BAND_MAX)))
                   for v, b in spec["anchors"]]
        acc += interp_band(vals[name], anchors) * spec["weight"]
        total_w += spec["weight"]
    return acc / total_w if total_w else float("nan")


def mae(sessions: list[dict], shifts: dict[str, float]) -> float:
    return float(np.mean([abs(predict(s["vals"], shifts) - s["known"])
                          for s in sessions]))


def fit(sessions: list[dict]) -> dict[str, float]:
    """Coordinate-wise exhaustive search (2 passes) over per-dimension shifts."""
    dims = list(RUBRIC)
    best = {d: 0.0 for d in dims}
    for _ in range(2):
        for d in dims:
            scores = []
            for s in SHIFTS:
                trial = {**best, d: float(s)}
                scores.append((mae(sessions, trial), float(s)))
            best[d] = min(scores)[1]
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    sessions = load_labeled()
    if len(sessions) < MIN_SESSIONS:
        print(f"Need >= {MIN_SESSIONS} sessions with a Known DUPR set; "
              f"found {len(sessions)}. Tag more sessions in the app first.")
        return 1

    base_mae = mae(sessions, {})
    shifts = fit(sessions)
    fit_mae = mae(sessions, shifts)

    print(f"labeled sessions: {len(sessions)}")
    print(f"MAE before: {base_mae:.3f}   after: {fit_mae:.3f}")
    print(f"shifts: { {k: v for k, v in shifts.items() if v} }")
    print(f"{'session':<28}{'known':>7}{'before':>8}{'after':>8}")
    for s in sessions:
        print(f"{(s['label'] or s['session'])[:27]:<28}{s['known']:>7.2f}"
              f"{predict(s['vals'], {}):>8.2f}{predict(s['vals'], shifts):>8.2f}")

    if args.dry_run:
        print("(dry run — nothing written)")
        return 0
    if fit_mae >= base_mae:
        print("fit did not improve MAE — not writing override")
        return 0
    out = {name: {"anchors": [[v, float(np.clip(b + shifts[name], BAND_MIN, BAND_MAX))]
                              for v, b in spec["anchors"]]}
           for name, spec in RUBRIC.items() if shifts.get(name)}
    CALIBRATED_PATH.parent.mkdir(exist_ok=True)
    CALIBRATED_PATH.write_text(json.dumps(out, indent=1))
    print(f"wrote {CALIBRATED_PATH} — new sessions will use calibrated anchors "
          "(re-analyze old sessions to update them)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
