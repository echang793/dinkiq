"""Player detection + tracking (YOLOv8-pose + ByteTrack) and subject selection.

Output: per-frame track rows (frame, track_id, x1, y1, x2, y2, conf, wrist
keypoints) plus the subject's projected court positions. Feet position =
bottom-center of bbox. Wrists feed M2 swing detection.
"""

from pathlib import Path

import numpy as np
import pandas as pd
from ultralytics import YOLO

from court import CourtCalibration, on_court

MODEL_NAME = "yolov8n-pose.pt"
# COCO keypoint indices
KP_LWRIST, KP_RWRIST = 9, 10

COLUMNS = ["frame", "track_id", "x1", "y1", "x2", "y2", "conf",
           "lwx", "lwy", "rwx", "rwy"]


TRACK_STRIDE = 2  # process every 2nd frame (15 fps effective); 1 = accuracy mode


def run_tracking(video: Path, out_parquet: Path, models_dir: Path,
                 progress_cb=None, frame_cb=None,
                 stride: int | None = None) -> pd.DataFrame:
    """Track all persons through the video; write tracks parquet.

    Single-decode integration: frame_cb(real_frame_idx, orig_img, boxes_xyxy)
    is invoked per processed frame so cheap consumers (ball detection) reuse
    the same decode instead of reading the video again. Frame numbers written
    to the parquet are REAL video frame indices (i * stride).
    """
    if stride is None:
        stride = TRACK_STRIDE  # resolved at call time so tests/config can override
    model = YOLO(str(models_dir / MODEL_NAME))
    rows = []
    # NOTE: do NOT pass half=True — on MPS it hits a deprecated fp16 fallback
    # that is ~27x SLOWER (measured 1430 ms/frame vs 53 ms/frame)
    results = model.track(
        source=str(video), stream=True, persist=True,
        classes=[0], tracker="bytetrack.yaml", verbose=False,
        vid_stride=stride,
    )
    for i, r in enumerate(results):
        real_f = i * stride
        xyxy = None
        if r.boxes is not None and r.boxes.id is not None:
            ids = r.boxes.id.cpu().numpy().astype(int)
            xyxy = r.boxes.xyxy.cpu().numpy()
            confs = r.boxes.conf.cpu().numpy()
            if r.keypoints is not None and r.keypoints.xy is not None:
                kps = r.keypoints.xy.cpu().numpy()  # (n, 17, 2); (0,0) = undetected
            else:
                kps = np.zeros((len(ids), 17, 2))
            for tid, box, conf, kp in zip(ids, xyxy, confs, kps):
                rows.append((real_f, tid, *box.tolist(), float(conf),
                             *kp[KP_LWRIST].tolist(), *kp[KP_RWRIST].tolist()))
        if frame_cb is not None:
            frame_cb(real_f, r.orig_img, xyxy)
        if progress_cb and i % 30 == 0:
            progress_cb(real_f)
    df = pd.DataFrame(rows, columns=COLUMNS)
    df.to_parquet(out_parquet, index=False)
    return df


def feet_px(df: pd.DataFrame) -> np.ndarray:
    """Bottom-center of each bbox: the feet contact point used for homography."""
    return np.stack([(df["x1"] + df["x2"]) / 2.0, df["y2"]], axis=1)


def pick_subject(df: pd.DataFrame, click_xy: tuple[float, float],
                 first_n_frames: int = 90) -> int:
    """Track id whose early bbox center is nearest the user's 'this is me' click."""
    early = df[df["frame"] <= df["frame"].min() + first_n_frames]
    if early.empty:
        raise ValueError("no tracks found in video")
    cx = (early["x1"] + early["x2"]) / 2.0
    cy = (early["y1"] + early["y2"]) / 2.0
    d2 = (cx - click_xy[0]) ** 2 + (cy - click_xy[1]) ** 2
    best = early.assign(d2=d2).groupby("track_id")["d2"].min()
    return int(best.idxmin())


MAX_STITCH_GAP = 45      # frames the subject may vanish before we stop following
STITCH_OVERLAP = 20      # a continuation may START this many frames BEFORE the
                         # current fragment ends (tracker fragments overlap)
STITCH_JUMP_PX = 12.0    # allowed bbox-center drift per missing frame
STITCH_BASE_PX = 60.0    # base allowance: an id switch mid-motion jumps a bit


def stitch_subject(df: pd.DataFrame, first_id: int) -> pd.DataFrame:
    """Follow the subject across track-id breaks by position continuity.

    Trackers fragment ids on occlusion or camera motion (broadcast footage
    produced 485 ids for 4 players), and the replacement id often starts a few
    frames BEFORE the old one dies. Starting from the clicked track, whenever
    the current fragment ends we adopt the unused track whose position at the
    handover point is nearest — searching a window that includes that overlap.
    """
    df = df.sort_values("frame")
    cx = (df["x1"] + df["x2"]) / 2.0
    cy = (df["y1"] + df["y2"]) / 2.0
    df = df.assign(cx=cx, cy=cy)
    by_id = {tid: g.reset_index(drop=True) for tid, g in df.groupby("track_id")}
    starts = {tid: int(g["frame"].iloc[0]) for tid, g in by_id.items()}

    chain = [first_id]
    used = {first_id}
    cur = by_id[first_id]
    while True:
        last = cur.iloc[-1]
        last_f = int(last["frame"])
        best_id, best_score = None, np.inf
        for tid, f0 in starts.items():
            if tid in used or not (last_f - STITCH_OVERLAP < f0 <= last_f + MAX_STITCH_GAP):
                continue
            g = by_id[tid]
            # candidate's position at the handover point (>= last_f if overlapping)
            at = g[g["frame"] >= last_f]
            head = at.iloc[0] if len(at) else g.iloc[-1]
            gap = max(0, f0 - last_f)
            d = np.hypot(head["cx"] - last["cx"], head["cy"] - last["cy"])
            if d <= STITCH_BASE_PX + STITCH_JUMP_PX * gap and d + 2.0 * gap < best_score:
                best_id, best_score = tid, d + 2.0 * gap
        if best_id is None:
            break
        used.add(best_id)
        chain.append(best_id)
        cur = by_id[best_id]

    out = df[df["track_id"].isin(chain)].drop(columns=["cx", "cy"])
    # a frame can appear in overlapping fragments — keep the first occurrence
    return out.sort_values("frame").drop_duplicates("frame").reset_index(drop=True)


def subject_court_positions(df: pd.DataFrame, subject_id: int,
                            calib: CourtCalibration, fps: float) -> pd.DataFrame:
    """Subject's feet in court feet-coords per frame, off-court points dropped.

    Follows the subject across tracker id fragmentation via stitch_subject.
    """
    sub = stitch_subject(df, subject_id)
    pts = calib.to_court(feet_px(sub))
    out = pd.DataFrame({
        "frame": sub["frame"].to_numpy(),
        "t": sub["frame"].to_numpy() / fps,
        "x": pts[:, 0],
        "y": pts[:, 1],
    })
    mask = [on_court(x, y) for x, y in zip(out["x"], out["y"])]
    return out[mask].reset_index(drop=True)
