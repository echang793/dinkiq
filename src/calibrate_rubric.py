"""Fit DUPR rubric anchors against sessions with a known DUPR rating.

Workflow: upload + calibrate each known-rating clip in the app, set its
"Known DUPR" in Session info, then run:

    .venv/bin/python src/calibrate_rubric.py                       # singles (default)
    .venv/bin/python src/calibrate_rubric.py --match-type doubles
    .venv/bin/python src/calibrate_rubric.py --dry-run             # report only

Fitting: per-dimension band SHIFT (piecewise anchors slide up/down together),
chosen by exhaustive grid search to minimize MAE of the weighted rubric
estimate vs known ratings. Deliberately low-parameter (one shift per
dimension) so a handful of clips can't overfit -- but "can't" isn't "doesn't":
the override is only written if the leave-one-out (held-out) error beats doing
nothing, since the in-sample score always flatters. Singles and doubles are
fitted separately, and known ratings above the rubric's ceiling are set aside.
Writes models/rubric_calibrated.json, which dupr.active_anchors() picks up for
that match type only.
"""

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dupr import BAND_MAX, BAND_MIN, CALIBRATED_PATH, RUBRIC, extract_dimension_values, interp_band
from jsonio import atomic_write_json, load_json
from pipeline import SESSIONS  # honours DINKIQ_DATA_DIR
MIN_SESSIONS = 4
SHIFTS = np.arange(-0.75, 0.80, 0.25)  # per-dimension band shift search space


def load_labeled(match_type: str | None = None) -> list[dict]:
    out = []
    for d in sorted(SESSIONS.iterdir() if SESSIONS.exists() else []):
        meta = load_json(d / "meta.json")
        metrics = load_json(d / "metrics.json")
        if not meta or not metrics or meta.get("known_dupr") is None:
            continue
        kind = meta.get("match_type") or metrics.get("match_type") or "singles"
        if match_type and kind != match_type:
            continue
        vals = extract_dimension_values(metrics, load_json(d / "events.json"), load_json(d / "shots.json"))
        if "nvz_discipline" not in vals:
            continue
        out.append({"session": d.name, "label": meta.get("label") or meta.get("filename"),
                    "known": float(meta["known_dupr"]), "match_type": kind, "vals": vals})
    return out


def split_beyond_range(sessions: list[dict]) -> tuple[list[dict], list[dict]]:
    """(usable, beyond): a known rating above the rubric's ceiling can't be fitted --
    every anchor is clipped at BAND_MAX, so it only drags all shifts upward."""
    return ([s for s in sessions if s["known"] <= BAND_MAX],
            [s for s in sessions if s["known"] > BAND_MAX])


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


def leave_one_out_mae(sessions: list[dict]) -> float:
    """Mean error when each session is predicted by shifts fitted WITHOUT it. This is the
    number that says whether the calibration generalises; the in-sample MAE does not."""
    errs = []
    for i, held in enumerate(sessions):
        rest = sessions[:i] + sessions[i + 1:]
        errs.append(abs(predict(held["vals"], fit(rest)) - held["known"]))
    return float(np.mean(errs))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--match-type", choices=("singles", "doubles"), default="singles")
    args = ap.parse_args(argv)

    sessions, beyond = split_beyond_range(load_labeled(args.match_type))
    for s in beyond:
        print(f"set aside {s['label'] or s['session']}: known {s['known']} is above the rubric ceiling ({BAND_MAX})")
    if len(sessions) < MIN_SESSIONS:
        print(f"Need >= {MIN_SESSIONS} {args.match_type} sessions with a Known DUPR set; "
              f"found {len(sessions)}. Tag more sessions in the app first.")
        return 1

    base_mae = mae(sessions, {})
    shifts = fit(sessions)
    fit_mae = mae(sessions, shifts)
    loo = leave_one_out_mae(sessions)

    print(f"labeled {args.match_type} sessions: {len(sessions)}")
    print(f"MAE  baseline: {base_mae:.3f}   in-sample after fit: {fit_mae:.3f}   held-out (leave-one-out): {loo:.3f}")
    print(f"shifts: { {k: v for k, v in shifts.items() if v} }")
    print(f"{'session':<28}{'known':>7}{'before':>8}{'after':>8}")
    for s in sessions:
        print(f"{(s['label'] or s['session'])[:27]:<28}{s['known']:>7.2f}"
              f"{predict(s['vals'], {}):>8.2f}{predict(s['vals'], shifts):>8.2f}")

    if args.dry_run:
        print("(dry run — nothing written)")
        return 0
    if loo >= base_mae:
        print("held-out error does not beat the uncalibrated rubric — the fit is likely "
              "overfitting; not writing an override")
        return 0
    out = {name: {"anchors": [[v, float(np.clip(b + shifts[name], BAND_MIN, BAND_MAX))]
                              for v, b in spec["anchors"]]}
           for name, spec in RUBRIC.items() if shifts.get(name)}
    out["_meta"] = {"match_type": args.match_type, "sessions": len(sessions),
                    "baseline_mae": round(base_mae, 3), "loo_mae": round(loo, 3)}
    CALIBRATED_PATH.parent.mkdir(exist_ok=True)
    atomic_write_json(CALIBRATED_PATH, out)
    print(f"wrote {CALIBRATED_PATH} for {args.match_type} — new sessions will use calibrated anchors "
          "(re-analyze old sessions to update them)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
