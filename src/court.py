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
