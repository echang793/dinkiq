"""Court geometry and pixel->court-coordinate homography.

Coordinate system (feet): x in [0, 20] across the width, y in [0, 44] along the
length. Net at y = 22. Non-volley-zone (kitchen) lines at y = 15 and y = 29.

Calibration corners are the four OUTER corners of the court, clicked in order:
far-left, far-right, near-right, near-left (as seen from the camera).
"""

import json
from pathlib import Path

import cv2
import numpy as np

COURT_W = 20.0
COURT_L = 44.0
NET_Y = 22.0
NVZ_DEPTH = 7.0  # kitchen line is 7 ft from the net on each side

# shared by points.py (in/out call) and shots.py (serve/landing lookup) so
# the two stages agree on which bounce belongs to which hit — they used to
# drift (2.0s vs 2.5s), which could make a shot plot as clearly out on the
# shot map while points.py reported no unforced error for the same rally
BOUNCE_LOOKUP_WINDOW_S = 2.5

# Court-space targets for the four clicked corners (same click order as docstring):
# far-left=(0,0), far-right=(20,0), near-right=(20,44), near-left=(0,44)
CORNER_TARGETS = np.array(
    [[0.0, 0.0], [COURT_W, 0.0], [COURT_W, COURT_L], [0.0, COURT_L]],
    dtype=np.float32,
)
# Kitchen (NVZ) line x sideline intersections, same click order:
# far-left=(0,15), far-right=(20,15), near-right=(20,29), near-left=(0,29)
KITCHEN_TARGETS = np.array(
    [[0.0, NET_Y - 7.0], [COURT_W, NET_Y - 7.0],
     [COURT_W, NET_Y + 7.0], [0.0, NET_Y + 7.0]],
    dtype=np.float32,
)


class CourtCalibration:
    """Homography from clicked reference points.

    4 outer corners are required. 4 kitchen-line intersections are optional but
    strongly improve accuracy: baseline corners are often estimated (off-frame
    or far away), while kitchen corners sit near the net where the lines are
    clearly visible — 8 correspondences give a least-squares fit anchored
    exactly in the zone that kitchen-time metrics depend on.
    """

    def __init__(self, corners_px: list[list[float]],
                 kitchen_px: list[list[float]] | None = None):
        if len(corners_px) != 4:
            raise ValueError("need exactly 4 corners")
        if kitchen_px is not None and len(kitchen_px) != 4:
            raise ValueError("kitchen_px must have exactly 4 points when given")
        self.corners_px = corners_px
        self.kitchen_px = kitchen_px
        src = np.array(corners_px, dtype=np.float32)
        if kitchen_px is None:
            self.H = cv2.getPerspectiveTransform(src, CORNER_TARGETS)
        else:
            src8 = np.vstack([src, np.array(kitchen_px, dtype=np.float32)])
            dst8 = np.vstack([CORNER_TARGETS, KITCHEN_TARGETS])
            self.H, _ = cv2.findHomography(src8, dst8, method=0)  # least squares
            if self.H is None:
                raise ValueError("degenerate calibration points")
            self.H = self.H.astype(np.float64)

    def to_court(self, points_px: np.ndarray) -> np.ndarray:
        """Map (N,2) pixel points to (N,2) court-feet coordinates."""
        pts = np.asarray(points_px, dtype=np.float32).reshape(-1, 1, 2)
        out = cv2.perspectiveTransform(pts, self.H)
        return out.reshape(-1, 2)

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(
            {"corners_px": self.corners_px, "kitchen_px": self.kitchen_px}))

    @classmethod
    def load(cls, path: Path) -> "CourtCalibration":
        data = json.loads(path.read_text())
        return cls(data["corners_px"], data.get("kitchen_px"))


def court_px_width(corners_px: list[list[float]]) -> float:
    """Average of the two baseline pixel lengths -- the pixel-to-feet scale
    for the session's actual camera framing. Screen-space speeds (ball mph,
    wrist-swing speed) are meaningless as raw px/s across sessions shot at
    different distances/zooms; dividing by this first makes them comparable
    (see shots.mph_from_norm)."""
    (flx, fly), (frx, fry), (nrx, nry), (nlx, nly) = corners_px
    return (np.hypot(frx - flx, fry - fly) + np.hypot(nrx - nlx, nry - nly)) / 2.0


def dist_from_net(y: float) -> float:
    return abs(y - NET_Y)


def zone_for(y: float) -> str:
    """Coarse positional zone by distance from the net.

    kitchen: at/inside NVZ line (+1ft buffer for foot placement noise)
    transition: classic "no man's land"
    baseline: back of the court (includes standing behind the baseline)
    """
    d = dist_from_net(y)
    if d <= NVZ_DEPTH + 1.0:
        return "kitchen"
    if d <= 15.0:
        return "transition"
    return "baseline"


def on_court(x: float, y: float, margin: float = 4.0) -> bool:
    """Whether a point is on or near the court (margin allows out-of-bounds play)."""
    return -margin <= x <= COURT_W + margin and -margin <= y <= COURT_L + margin


def on_court_mask(points: np.ndarray, margin: float = 4.0,
                  margin_y: float | None = None) -> np.ndarray:
    """Vectorised on_court for an (N, 2) array of court-feet points. margin_y, when
    given, is the allowance past each BASELINE (the court's length axis) separately
    from the sideline margin: players routinely stand several feet behind it."""
    pts = np.asarray(points, dtype=float).reshape(-1, 2)
    my = margin if margin_y is None else margin_y
    return ((pts[:, 0] >= -margin) & (pts[:, 0] <= COURT_W + margin)
            & (pts[:, 1] >= -my) & (pts[:, 1] <= COURT_L + my))


# How far past the court boundary a bounce can plausibly be and still be a real shot
# landing, as opposed to a glare/reflection/spectator misdetection the classical ball
# detector locked onto. points.py's OUT_MARGIN (0.5 ft) is the strict in/out call; this
# just rejects bounces that can't belong to this court at all. (Was 15 ft: x=34 and
# y=-11 passed on the sample.)
PLAUSIBLE_BOUNCE_MARGIN = 8.0


def first_bounce_after(bounces, t0: float, until: float | None = None,
                       margin: float = PLAUSIBLE_BOUNCE_MARGIN):
    """The earliest plausible bounce after a hit at t0, or None.

    The one lookup shared by serve landings, opponent landings and rally outcomes
    (they used three slightly different ones and could disagree about the same
    shot). The window is BOUNCE_LOOKUP_WINDOW_S, but never past `until` -- pass the
    time of the NEXT hit, because a bounce after that belongs to that shot."""
    if bounces is None or not len(bounces):
        return None
    hi = t0 + BOUNCE_LOOKUP_WINDOW_S if until is None else min(t0 + BOUNCE_LOOKUP_WINDOW_S, until)
    b = bounces[(bounces["t"] > t0) & (bounces["t"] < hi)]
    if not len(b):
        return None
    ok = b[on_court_mask(b[["x", "y"]].to_numpy(), margin)]
    return ok.sort_values("t").iloc[0] if len(ok) else None
