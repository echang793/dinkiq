"""M3 ball tracking — classical motion-blob tracker, no learned model.

A pickleball is a small (~3in) fast bright object. Strategy: MOG2 background
subtraction -> small round foreground blobs -> nearest-neighbor linking into
trajectories with a velocity gate. Monocular, so ball position is only court-
meaningful at BOUNCES (ball on ground plane -> homography valid there).

Explicitly degradable: downstream consumers check `coverage` before using
ball-derived metrics.
"""

from pathlib import Path

import cv2
import numpy as np
import pandas as pd

MIN_AREA_PX = 6        # blob area bounds at 720p
MAX_AREA_PX = 400
MAX_JUMP_PX = 120      # max per-frame travel (720p, 30fps)
MIN_TRACK_LEN = 5      # shorter linked runs are noise
MAX_GAP_FRAMES = 3     # allow short detection dropouts within a track

# color-plausibility gate: pickleballs are small bright objects (optic
# yellow/green/orange, occasionally white) -- reject dark blobs outright
# (shadows, dark clothing edges, wood-grain patches that happen to pass the
# size/shape checks), and reject saturated blobs whose hue doesn't match a
# ball color (skin tone, a jersey). Low-saturation bright blobs are never
# hue-checked: compression/motion blur routinely washes out the true color,
# and pickleballs legitimately come in white too -- err toward not
# rejecting a real ball over cutting more false positives.
MIN_BALL_V = 140
BALL_SAT_GATE = 60
BALL_HUE_LO, BALL_HUE_HI = 20, 100  # OpenCV hue 0-179: yellow(30) through green(60) to cyan-ish


def _ball_colored(frame: np.ndarray, x: int, y: int, w: int, h: int) -> bool:
    crop = frame[max(0, y):y + h, max(0, x):x + w]
    if crop.size == 0:
        return False
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    if float(np.median(hsv[:, :, 2])) < MIN_BALL_V:
        return False
    if float(np.median(hsv[:, :, 1])) < BALL_SAT_GATE:
        return True  # bright + washed-out: still plausible (white ball, blur)
    return BALL_HUE_LO <= float(np.median(hsv[:, :, 0])) <= BALL_HUE_HI


class BallDetector:
    """Frame-wise ball-candidate detector for single-decode integration.

    Feed it every decoded frame (e.g. from the YOLO tracking loop via
    result.orig_img) with that frame's player boxes; it accumulates
    (frame, x, y, area) candidates. Blobs inside player boxes are
    limbs/paddles, not the ball.
    """

    def __init__(self):
        self._mog = cv2.createBackgroundSubtractorMOG2(
            history=120, varThreshold=24, detectShadows=False)
        self._kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        self.candidates: list[tuple] = []
        self.frames_seen = 0
        self.last_frame_idx = -1

    def update(self, frame_idx: int, frame: np.ndarray,
               boxes: np.ndarray | None = None) -> None:
        self.frames_seen += 1
        self.last_frame_idx = max(self.last_frame_idx, frame_idx)
        mask = self._mog.apply(frame)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self._kernel)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in contours:
            area = cv2.contourArea(c)
            if not (MIN_AREA_PX <= area <= MAX_AREA_PX):
                continue
            x, y, w, h = cv2.boundingRect(c)
            if max(w, h) > 3.5 * max(1, min(w, h)):  # too elongated: not a ball
                continue
            cx, cy = x + w / 2.0, y + h / 2.0
            if boxes is not None and len(boxes):
                inside = ((boxes[:, 0] - 5 <= cx) & (cx <= boxes[:, 2] + 5) &
                          (boxes[:, 1] - 5 <= cy) & (cy <= boxes[:, 3] + 5))
                if inside.any():
                    continue
            if not _ball_colored(frame, x, y, w, h):
                continue
            self.candidates.append((frame_idx, cx, cy, area))


class CutDetector:
    """Camera-cut detector fed from the same decode loop (frame_cb consumer).

    Broadcast footage cuts between angles; each cut resets tracking context and
    invalidates the court homography for the new angle. HSV-histogram
    correlation between consecutive processed frames collapses on a hard cut.
    """

    CORR_THRESHOLD = 0.6

    def __init__(self):
        self._prev = None
        self.cut_frames: list[int] = []

    def update(self, frame_idx: int, frame: np.ndarray) -> None:
        small = cv2.resize(frame, (160, 90))
        hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [24, 16], [0, 180, 0, 256])
        cv2.normalize(hist, hist)
        if self._prev is not None:
            corr = cv2.compareHist(self._prev, hist, cv2.HISTCMP_CORREL)
            if corr < self.CORR_THRESHOLD:
                self.cut_frames.append(frame_idx)
        self._prev = hist


def detect_candidates(video: Path, exclude_boxes: pd.DataFrame | None = None):
    """Standalone per-frame candidate pass (own decode). The integrated
    pipeline uses BallDetector inside the tracking loop instead; this path
    remains for tests and ad-hoc analysis."""
    boxes_by_frame: dict[int, np.ndarray] = {}
    if exclude_boxes is not None and len(exclude_boxes):
        for f, g in exclude_boxes.groupby("frame"):
            boxes_by_frame[int(f)] = g[["x1", "y1", "x2", "y2"]].to_numpy()

    det = BallDetector()
    cap = cv2.VideoCapture(str(video))
    i = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        det.update(i, frame, boxes_by_frame.get(i))
        i += 1
    cap.release()
    return det.candidates, i  # candidates, total frames


def link_tracks(cands: list[tuple], total_frames: int, stride: int = 1) -> pd.DataFrame:
    """Greedy nearest-neighbor linking with velocity gate and gap tolerance.

    stride: source sampling stride (frame numbers are real video frames, so
    gap/velocity tolerances scale by it).
    """
    by_frame: dict[int, list] = {}
    for f, x, y, a in cands:
        by_frame.setdefault(f, []).append((x, y))
    max_gap = MAX_GAP_FRAMES * stride

    active: list[dict] = []   # {pts: [(f,x,y)], last_f}
    done: list[list] = []
    for f in range(0, total_frames, stride):
        pts = by_frame.get(f, [])
        used = set()
        for tr in active:
            lf, lx, ly = tr["pts"][-1]
            best_j, best_d = -1, MAX_JUMP_PX * (f - lf)
            for j, (x, y) in enumerate(pts):
                if j in used:
                    continue
                d = np.hypot(x - lx, y - ly)
                if d < best_d:
                    best_j, best_d = j, d
            if best_j >= 0:
                x, y = pts[best_j]
                tr["pts"].append((f, x, y))
                used.add(best_j)
        # retire stale tracks, start new ones from unmatched detections
        still = []
        for tr in active:
            if f - tr["pts"][-1][0] > max_gap:
                if len(tr["pts"]) >= MIN_TRACK_LEN:
                    done.append(tr["pts"])
            else:
                still.append(tr)
        active = still
        for j, (x, y) in enumerate(pts):
            if j not in used:
                active.append({"pts": [(f, x, y)]})
    for tr in active:
        if len(tr["pts"]) >= MIN_TRACK_LEN:
            done.append(tr["pts"])

    rows = [(f, x, y, ti) for ti, pts in enumerate(done) for f, x, y in pts]
    return pd.DataFrame(rows, columns=["frame", "x", "y", "seg"])


def detect_bounces(ball: pd.DataFrame, fps: float) -> pd.DataFrame:
    """Bounce = local maximum of screen-y (ball lowest on screen) inside a segment.

    At the bounce instant the ball touches the ground plane, so homography
    projection is valid there — the only place monocular court coords work.
    """
    rows = []
    for _, seg in ball.groupby("seg"):
        seg = seg.sort_values("frame").reset_index(drop=True)
        y = seg["y"].to_numpy()
        for i in range(2, len(seg) - 2):
            lo, hi = max(0, i - 4), min(len(y), i + 5)
            prominence = y[i] - min(y[lo:hi])  # vertex height over ±4-frame window
            if y[i] >= y[i - 1] and y[i] >= y[i + 1] and prominence > 4:
                rows.append((int(seg["frame"][i]), float(seg["x"][i]), float(y[i])))
    df = pd.DataFrame(rows, columns=["frame", "x", "y"])
    if len(df):
        df["t"] = df["frame"] / fps
    return df


def build_ball_track(cands: list[tuple], total_frames: int, out_parquet: Path,
                     stride: int = 1) -> tuple[pd.DataFrame, dict]:
    """Link candidates (from BallDetector or detect_candidates) into ball.parquet."""
    ball = link_tracks(cands, total_frames, stride=stride)
    ball.to_parquet(out_parquet, index=False)
    sampled = max(1, total_frames // stride)
    coverage = round(float(ball["frame"].nunique()) / sampled, 3) if len(ball) else 0.0
    stats = {"coverage": coverage, "segments": int(ball["seg"].nunique()) if len(ball) else 0,
             "frames": total_frames, "stride": stride}
    return ball, stats


def run_ball_tracking(video: Path, tracks: pd.DataFrame, out_parquet: Path,
                      fps: float) -> tuple[pd.DataFrame, dict]:
    """Standalone decode + link (tests / ad-hoc); pipeline uses the integrated path."""
    cands, total = detect_candidates(video, tracks)
    return build_ball_track(cands, total, out_parquet)
